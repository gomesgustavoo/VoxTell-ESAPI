"""Prometheus metrics for the control plane.

Before this there was no instrumentation at all — no ``/metrics``, no counters, no
structured logging. Every failure mode the queue can hit was invisible: a queue that
had stopped draining, a pool that had saturated, a user being turned away, the GPU
mutex held by a wedged job. You could only find them by reading rows by hand.

Three rules here are load-bearing, not style:

**1. No ``user_id`` label. Ever.**
One series per user, retained for the whole retention window, growing without bound —
and this is a multi-tenant medical product, so a label value is also a weak identifier
sitting in a store with no access control. Per-user facts are exposed as *aggregates
over* users instead (``voxtell_queue_depth_per_user_max``,
``voxtell_users_with_outstanding_jobs``). "Which user" is a SQL question, and
``tests/test_metrics_endpoint.py`` asserts this mechanically so it cannot drift.

**2. DB-derived gauges are the same truth reported twice.**
The API runs ``replicas: 2``, and both replicas read the same rows. Those series must
be aggregated with ``max()`` in every query — ``sum()`` would double the queue depth
on every dashboard. They carry a ``_current`` suffix so the distinction is visible at
the call site. Process-local counters and histograms are per-process and *do* need
``sum()``.

**3. Never query the database on the scrape thread.**
A slow Postgres would otherwise turn into a scrape timeout, so the monitoring stack
goes blind exactly when you need it. The DB gauges are refreshed by a background task
on its own cadence and the scrape only reads memory. Degrading to slightly stale
numbers is strictly better than degrading to no numbers.

The endpoint is reachable from the internet (it lives under ``/v1`` so the existing
single-hostname path split routes it with no Ingress change), so it is gated on a
shared token. Queue depth and tenant counts are business intelligence; they are not
public.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from . import API_VERSION, db
from .config import settings

log = logging.getLogger(__name__)

# A private registry rather than the global default: the default picks up the
# process/platform/gc collectors, and prometheus_client's multiprocess story is
# awkward. One explicit registry keeps the exposition predictable.
REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------- process series
#
# Counters and histograms. Per-process, so aggregate with sum() across replicas.

HTTP_REQUESTS = Counter(
    "voxtell_http_requests_total",
    "HTTP requests by route template and status.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "voxtell_http_request_seconds",
    "HTTP request duration by route template.",
    ["method", "route"],
    # Tuned to this API: most routes are a couple of queries, but /submit performs a
    # multi-second S3 CompleteMultipartUpload, so the top buckets matter.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
ADMISSION_REJECTIONS = Counter(
    "voxtell_admission_rejections_total",
    "Jobs refused at admission, by reason. 'Are we turning users away, and why.'",
    ["reason"],
    registry=REGISTRY,
)
RETRY_AFTER = Histogram(
    "voxtell_retry_after_seconds",
    "Retry-After values handed out. Watch this to see whether the estimator is sane.",
    buckets=(5, 15, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
RECLAIM_ACTIONS = Counter(
    "voxtell_reclaim_actions_total",
    "Jobs recovered or failed by the reclaim loop, by action. A rising rate means "
    "something is dying repeatedly.",
    ["action"],
    registry=REGISTRY,
)
SWEEP_ACTIONS = Counter(
    "voxtell_sweep_actions_total",
    "Retention actions taken, by kind.",
    ["kind"],
    registry=REGISTRY,
)
SCHEMA_MIGRATION_FAILURES = Counter(
    "voxtell_schema_migration_failures_total",
    "Migrations that failed at startup. Any increase is critical — the API may be "
    "serving against a half-migrated schema.",
    registry=REGISTRY,
)
DB_POOL_IN_USE = Gauge(
    "voxtell_db_pool_in_use",
    "Connections checked out of this replica's SQLAlchemy pool. The ceiling is "
    "pool_size + max_overflow, and hitting it 500s every route including /v1/health.",
    registry=REGISTRY,
)
DB_POOL_OVERFLOW = Gauge(
    "voxtell_db_pool_overflow",
    "Overflow connections in use beyond pool_size on this replica.",
    registry=REGISTRY,
)
BUILD_INFO = Gauge(
    "voxtell_build_info",
    "Always 1; the labels carry the version. Graph changes in this as deploy "
    "annotations so a regression lines up with an image bump.",
    ["version", "component"],
    registry=REGISTRY,
)
BUILD_INFO.labels(version=API_VERSION, component="api").set(1)


def observe_rejection(reason: str, retry_after: int | None = None) -> None:
    """Record an admission refusal. Called from quota.admit()."""
    ADMISSION_REJECTIONS.labels(reason=reason).inc()
    if retry_after is not None:
        RETRY_AFTER.observe(retry_after)


def observe_reclaim(actions: dict[str, int]) -> None:
    for action, count in actions.items():
        if count:
            RECLAIM_ACTIONS.labels(action=action).inc(count)


# ------------------------------------------------------------------- DB gauges
#
# A custom collector holding a snapshot refreshed off the scrape path. Names end in
# `_current` as a reminder to aggregate with max(), not sum(), across replicas.

# One query, not seven: this runs every refresh on both replicas, and a single
# round trip keeps that negligible. Every branch is covered by a partial index.
_SNAPSHOT_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM jobs WHERE state = 'awaiting_upload') AS jobs_awaiting_upload,
      (SELECT count(*) FROM jobs WHERE state = 'queued')          AS jobs_queued,
      (SELECT count(*) FROM jobs WHERE state = 'running')         AS jobs_running,
      (SELECT coalesce(max(EXTRACT(EPOCH FROM (now() - coalesce(queued_at, created_at)))), 0)
         FROM jobs WHERE state = 'queued')                        AS oldest_queued_age,
      (SELECT coalesce(max(cnt), 0) FROM (
            SELECT count(*) AS cnt FROM jobs
             WHERE state IN ('queued','running') GROUP BY user_id
       ) per_user)                                                AS depth_per_user_max,
      (SELECT count(*) FROM (
            SELECT 1 FROM jobs WHERE state IN ('queued','running') GROUP BY user_id
       ) busy)                                                    AS users_with_outstanding,
      (SELECT count(*) FROM volumes WHERE state = 'ready')        AS volumes_ready,
      (SELECT count(*) FROM volumes WHERE state = 'uploading')    AS volumes_uploading,
      (SELECT coalesce(sum(bytes), 0) FROM volumes
         WHERE state IN ('ready','uploading'))                     AS volume_bytes,
      (SELECT count(*) FROM usage_events
         WHERE created_at >= date_trunc('month', now()))          AS usage_month,
      (SELECT count(*) FROM prompt_embeddings)                    AS cached_prompts,
      (SELECT count(*) FROM jobs
         WHERE state = 'running' AND lease_expires_at IS NOT NULL
           AND lease_expires_at < now())                          AS leases_expired
    """
)

# Column -> (metric name, help). Kept declarative so adding a number is one row here
# plus one column above, with no collector plumbing.
_GAUGE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("jobs_awaiting_upload", "voxtell_jobs_awaiting_upload_current", "Jobs waiting for bytes."),
    ("jobs_queued", "voxtell_jobs_queued_current", "Jobs waiting for the GPU."),
    ("jobs_running", "voxtell_jobs_running_current", "Jobs on the GPU."),
    (
        "oldest_queued_age",
        "voxtell_queue_oldest_queued_age_seconds",
        "Age of the longest-waiting queued job. Climbing while running==0 means the "
        "queue has stopped draining.",
    ),
    (
        "depth_per_user_max",
        "voxtell_queue_depth_per_user_max",
        "Largest single user's outstanding count. Tracking the cap while several "
        "users are active is the signature of unfair dispatch.",
    ),
    (
        "users_with_outstanding",
        "voxtell_users_with_outstanding_jobs",
        "Distinct users with queued or running work — concurrency, without a user_id label.",
    ),
    ("volumes_ready", "voxtell_volumes_ready_current", "Uploaded series held and reusable."),
    ("volumes_uploading", "voxtell_volumes_uploading_current", "Series mid-upload."),
    (
        "volume_bytes",
        "voxtell_volume_bytes_current",
        "Bytes of held series. Watch against the SeaweedFS PVC size.",
    ),
    ("usage_month", "voxtell_usage_events_month_current", "Jobs booked this calendar month."),
    (
        "cached_prompts",
        "voxtell_prompt_embeddings_current",
        "Rows in the embedding cache. This sat at 0 for a week while the worker's "
        "INSERT violated NOT NULL, so every novel prompt reloaded an 8 GB backbone.",
    ),
    (
        "leases_expired",
        "voxtell_jobs_lease_expired_current",
        "Running jobs whose lease has already lapsed and are awaiting reclaim. "
        "Should be ~0; a sustained value means the reclaim loop is not running.",
    ),
)


class _DatabaseSnapshot:
    """Holds the last DB read and yields it to scrapes without touching Postgres."""

    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._fresh = False
        self._age_seconds = 0.0
        self._refreshed_monotonic = 0.0

    async def refresh(self) -> None:
        async with db.SessionLocal() as session:
            row = (await session.execute(_SNAPSHOT_SQL)).mappings().one()
        self._values = {key: float(row[key] or 0) for key in row.keys()}
        self._fresh = True
        self._refreshed_monotonic = time.monotonic()

    def mark_stale(self) -> None:
        self._fresh = False

    def collect(self) -> Iterable[GaugeMetricFamily]:
        # Always emit this one, so a dashboard can tell "no queued jobs" apart from
        # "the collector cannot reach the database" — which otherwise look identical.
        ok = GaugeMetricFamily(
            "voxtell_db_snapshot_ok",
            "1 when the DB-derived gauges below are fresh, 0 when the last refresh failed.",
            value=1.0 if self._fresh else 0.0,
        )
        yield ok
        age = time.monotonic() - self._refreshed_monotonic if self._refreshed_monotonic else 0.0
        yield GaugeMetricFamily(
            "voxtell_db_snapshot_age_seconds",
            "Seconds since the DB-derived gauges were last refreshed.",
            value=age,
        )
        if not self._values:
            return
        for column, name, documentation in _GAUGE_SPECS:
            if column in self._values:
                yield GaugeMetricFamily(name, documentation, value=self._values[column])


    def public_view(self) -> dict[str, float | bool]:
        """The subset ``GET /v1/system`` may show a signed-in user.

        An accessor rather than letting the route read ``_values`` directly, so the
        gauge set can grow without deciding by accident what is public. Queue depth
        and running count are the caller's own wait, explained; per-user maxima and
        tenant counts stay in Prometheus, behind its token.

        Deliberately serves the CACHE. A dashboard polling this every few seconds
        must not be able to put load on Postgres — which is the whole reason this
        snapshot exists.
        """
        v = self._values
        queued = int(v.get("jobs_queued", 0))
        running = int(v.get("jobs_running", 0))
        oldest = float(v.get("oldest_queued_age", 0.0))
        expired = int(v.get("leases_expired", 0))

        # Offline means "nothing is picking work up", and the two ways to see that
        # are a lapsed lease on a running job, or queued work sitting with an idle
        # GPU. A job merely *waiting for the GPU mutex* held by DicomSegVR is
        # already `running` — the worker claims it and then blocks — so it does not
        # trip this. The 180 s grace absorbs the gap between claims.
        stalled_queue = queued > 0 and running == 0 and oldest > 180
        return {
            "queue_depth": queued,
            "running": running,
            "worker_online": self._fresh and not expired and not stalled_queue,
            "snapshot_age_seconds": round(
                time.monotonic() - self._refreshed_monotonic
                if self._refreshed_monotonic
                else 0.0,
                1,
            ),
        }


SNAPSHOT = _DatabaseSnapshot()


class _SnapshotCollector:
    def collect(self):  # noqa: ANN201 - prometheus_client's duck-typed interface
        return SNAPSHOT.collect()


REGISTRY.register(_SnapshotCollector())


def _record_pool() -> None:
    """Pool occupancy, read straight off the engine — no query needed."""
    try:
        pool = db.engine.pool
        DB_POOL_IN_USE.set(pool.checkedout())
        DB_POOL_OVERFLOW.set(max(0, pool.overflow()))
    except Exception:  # pragma: no cover - a metric must never break a scrape
        pass


async def refresh_loop() -> None:
    """Keep the snapshot warm, off the scrape path. Started from the lifespan."""
    while True:
        try:
            await SNAPSHOT.refresh()
            _record_pool()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            SNAPSHOT.mark_stale()
            log.warning("metrics refresh failed: %s", exc)
        await asyncio.sleep(settings.VOXTELL_METRICS_REFRESH_SECONDS)


def render() -> bytes:
    _record_pool()
    return generate_latest(REGISTRY)

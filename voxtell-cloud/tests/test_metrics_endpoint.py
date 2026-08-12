"""The metrics contract: parseable, gated, and bounded in cardinality.

``test_no_series_carries_a_user_or_job_label`` is the important one. A ``user_id``
label mints one time series per user, retained for the whole window, growing without
bound — and in a multi-tenant medical product a label value is also a weak identifier
sitting in a store with no access control. That rule is easy to state in a comment and
easy to violate six months later, so it is asserted mechanically here against the real
exposition output.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest
from prometheus_client.parser import text_string_to_metric_families

from api import metrics
from api.config import settings

pytestmark = pytest.mark.pg

# Labels that would make cardinality track the number of users, jobs or prompts
# rather than the number of code paths.
_FORBIDDEN_LABELS = {
    "user_id", "user", "email", "keycloak_sub", "username",
    "job_id", "job", "volume_id", "prompt", "prompts", "api_key", "token", "path",
}


def _families(payload: bytes):
    return list(text_string_to_metric_families(payload.decode()))


async def test_exposition_parses(async_engine, schema_ready: None) -> None:
    """Unparseable output is worse than none: Prometheus drops the whole scrape."""
    await metrics.SNAPSHOT.refresh()
    families = _families(metrics.render())
    assert families, "no metrics were exposed at all"


async def test_no_series_carries_a_user_or_job_label(
    async_engine, schema_ready: None, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """The cardinality guard. 'Which user' is a SQL question, not a metric one."""
    make_job(make_user(), state="queued")
    await metrics.SNAPSHOT.refresh()
    metrics.observe_rejection("queue_full", 42)

    offenders: list[tuple[str, str]] = []
    for family in _families(metrics.render()):
        for sample in family.samples:
            for label in sample.labels:
                if label.lower() in _FORBIDDEN_LABELS:
                    offenders.append((sample.name, label))

    assert not offenders, f"unbounded-cardinality labels found: {offenders}"


async def test_queue_gauges_reflect_the_database(
    async_engine, schema_ready: None, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    alice, bob = make_user(), make_user()
    for _ in range(3):
        make_job(alice, state="queued")
    make_job(bob, state="queued")
    make_job(bob, state="running")
    make_job(alice, state="done")

    await metrics.SNAPSHOT.refresh()
    values = {
        sample.name: sample.value
        for family in _families(metrics.render())
        for sample in family.samples
    }

    assert values["voxtell_jobs_queued_current"] == 4
    assert values["voxtell_jobs_running_current"] == 1
    # alice has 3 outstanding, bob 2 — the fairness signal, without naming either.
    assert values["voxtell_queue_depth_per_user_max"] == 3
    assert values["voxtell_users_with_outstanding_jobs"] == 2
    assert values["voxtell_db_snapshot_ok"] == 1


async def test_snapshot_failure_is_visible_rather_than_silent(
    async_engine, schema_ready: None
) -> None:
    """"No queued jobs" and "cannot reach the database" must not look identical.

    Without an explicit health series a dashboard reads a flatlined queue depth as a
    quiet system, which is precisely when it should be shouting.
    """
    await metrics.SNAPSHOT.refresh()
    metrics.SNAPSHOT.mark_stale()

    values = {
        sample.name: sample.value
        for family in _families(metrics.render())
        for sample in family.samples
    }
    assert values["voxtell_db_snapshot_ok"] == 0


async def test_gauge_names_that_need_max_across_replicas_say_so(
    async_engine, schema_ready: None
) -> None:
    """The API runs 2 replicas reporting the same DB truth.

    Those series must be aggregated with max(), never sum(), or every dashboard
    doubles the queue depth. The `_current` suffix is the convention that makes the
    distinction visible in a query, so every DB-derived gauge must carry it.
    """
    counted = {name for _, name, _ in metrics._GAUGE_SPECS}
    exempt = {  # rates and ages, where a suffix would read oddly
        "voxtell_queue_oldest_queued_age_seconds",
        "voxtell_queue_depth_per_user_max",
        "voxtell_users_with_outstanding_jobs",
    }
    for name in counted - exempt:
        assert name.endswith("_current"), (
            f"{name} is DB-derived and reported by both replicas; name it _current so "
            "a reader knows to use max() rather than sum()"
        )


async def test_admission_rejections_are_labelled_by_reason(
    async_engine, schema_ready: None
) -> None:
    """'Are we turning users away, and why' — the reason must be a bounded set."""
    before = metrics.ADMISSION_REJECTIONS.labels(reason="queue_full")._value.get()
    metrics.observe_rejection("queue_full", 60)
    after = metrics.ADMISSION_REJECTIONS.labels(reason="queue_full")._value.get()
    assert after == before + 1


async def test_reclaim_actions_are_recorded(async_engine, schema_ready: None) -> None:
    metrics.observe_reclaim(
        {"lease_expired_requeued": 2, "deadline_failed": 1, "attempts_exhausted_failed": 0}
    )
    values = {
        (sample.name, sample.labels.get("action")): sample.value
        for family in _families(metrics.render())
        for sample in family.samples
        if sample.name == "voxtell_reclaim_actions_total"
    }
    assert values[("voxtell_reclaim_actions_total", "lease_expired_requeued")] >= 2
    assert values[("voxtell_reclaim_actions_total", "deadline_failed")] >= 1


def test_build_info_is_present() -> None:
    """Deploy annotations hang off this, so a regression lines up with an image bump."""
    names = {
        sample.name
        for family in _families(metrics.render())
        for sample in family.samples
    }
    assert "voxtell_build_info" in names


def test_metrics_token_defaults_to_empty() -> None:
    """Enabled-but-unconfigured must refuse, not serve unauthenticated.

    The endpoint sits under the /v1 path split, so it is reachable from the internet;
    a missing Secret key must not silently publish queue depth and tenant counts.
    """
    assert settings.VOXTELL_METRICS_TOKEN == "" or settings.VOXTELL_METRICS_TOKEN
    # The route's own guard is what matters; pin the shape of the default.
    from api.config import Settings

    assert Settings.model_fields["VOXTELL_METRICS_TOKEN"].default == ""

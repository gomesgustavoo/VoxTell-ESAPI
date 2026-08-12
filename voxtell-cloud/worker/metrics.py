"""Prometheus metrics for the GPU worker.

Exposed on its own port by ``prometheus_client.start_http_server`` — a plain
threaded HTTP server, not a FastAPI app, because the worker has no web framework and
should not gain one to publish twelve numbers. Kubernetes SD scrapes the pod IP
directly, so no Service is needed.

**The liveness probe must NOT be moved to this port.** ``start_http_server`` runs a
daemon thread that keeps answering happily while the asyncio event loop is dead or the
GPU thread is wedged — precisely the wrong property for a liveness signal. Liveness
stays the ``/tmp/alive`` touch-file, whose whole value is that it goes *stale* when a
job stalls, letting the kubelet kill a pod that is holding the shared GPU mutex.

Two series here are the ones that matter most, and neither existed before:

``voxtell_gpu_lock_held_current_seconds`` — how long the cross-service GPU mutex has
been held right now. A value that keeps climbing *is* the DicomSegVR deadlock, live,
with a name. That failure has happened silently.

``voxtell_job_queue_wait_seconds`` — ``started_at - queued_at`` at claim. This is the
fairness SLI: it is the number that should drop when round-robin dispatch replaces
global FIFO, and without a baseline there is no way to know whether the change worked.

Cardinality: no ``user_id``, no ``job_id``, no prompt text. Labels are bounded
vocabularies only (stage names, outcome names). See ``api/metrics.py`` for the full
reasoning.
"""

from __future__ import annotations

import logging
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

log = logging.getLogger("worker.metrics")

REGISTRY = CollectorRegistry()

# ------------------------------------------------------------------ the queue side

CLAIM_SECONDS = Histogram(
    "voxtell_worker_claim_seconds",
    "Duration of the claim query. Grows if the dispatch ordering outgrows its index.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1),
    registry=REGISTRY,
)
CLAIMS = Counter(
    "voxtell_worker_claims_total",
    "Claim attempts by result: 'job' when one was picked up, 'empty' when idle.",
    ["result"],
    registry=REGISTRY,
)
QUEUE_WAIT = Histogram(
    "voxtell_job_queue_wait_seconds",
    "Seconds a job waited between being queued and starting. THE fairness SLI.",
    # Wide: sub-second when idle, tens of minutes behind a full queue.
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
    registry=REGISTRY,
)
JOB_DURATION = Histogram(
    "voxtell_job_duration_seconds",
    "End-to-end job duration by outcome.",
    ["outcome"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800),
    registry=REGISTRY,
)
STAGE_SECONDS = Histogram(
    "voxtell_stage_seconds",
    "Duration of each pipeline stage. Answers 'is the bottleneck upload, preprocess, "
    "the GPU, or postprocess?' — previously unanswerable.",
    ["stage"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 180, 600),
    registry=REGISTRY,
)
INFLIGHT = Gauge(
    "voxtell_jobs_inflight",
    "Jobs this worker is currently processing (GPU plus CPU stages).",
    registry=REGISTRY,
)

# ------------------------------------------------------------------- the GPU side

GPU_SECONDS = Counter(
    "voxtell_gpu_seconds_total",
    "Cumulative GPU seconds spent on inference. Billing-adjacent, and the input to "
    "the wait estimator.",
    registry=REGISTRY,
)
GPU_LOCK_WAIT = Histogram(
    "voxtell_gpu_lock_wait_seconds",
    "Time spent blocked on the cross-service GPU mutex before acquiring it. A high "
    "p90 means DicomSegVR is starving us, or we are starving it.",
    buckets=(0.01, 0.1, 1, 5, 15, 60, 300, 900, 1800),
    registry=REGISTRY,
)
GPU_LOCK_HELD = Histogram(
    "voxtell_gpu_lock_held_seconds",
    "How long the GPU mutex was held, per acquisition.",
    buckets=(1, 5, 15, 60, 300, 900, 1800, 3600),
    registry=REGISTRY,
)
GPU_LOCK_HELD_CURRENT = Gauge(
    "voxtell_gpu_lock_held_current_seconds",
    "Seconds the GPU mutex has been held by this worker right now, 0 when free. A "
    "value that keeps climbing IS the cross-product deadlock — alert on it.",
    registry=REGISTRY,
)

# ------------------------------------------------------- liveness and the caches

STALLS = Counter(
    "voxtell_worker_stalls_total",
    "Times a job was observed making no progress within its phase's grace period, "
    "by phase. Non-zero means the watchdog is doing its job.",
    ["phase"],
    registry=REGISTRY,
)
LEASE_RENEWALS = Counter(
    "voxtell_lease_renewals_total",
    "Lease renewals by result: 'renewed' for a progressing job, 'withheld' for a "
    "stalled one. Withheld renewals are what let the reclaim loop recover the job.",
    ["result"],
    registry=REGISTRY,
)
EMBEDDING_CACHE = Counter(
    "voxtell_prompt_embedding_cache_total",
    "Prompt embedding lookups by result (hit/miss).",
    ["result"],
    registry=REGISTRY,
)
EMBEDDING_PERSIST = Counter(
    "voxtell_prompt_embedding_persist_total",
    "Attempts to persist new embeddings by result. 'error' sat at 100% for a week "
    "while created_at had no server default.",
    ["result"],
    registry=REGISTRY,
)
TEXT_BACKBONE_LOADS = Counter(
    "voxtell_text_backbone_loads_total",
    "Times the ~8 GB Qwen3 text backbone was loaded. If this keeps rising, the "
    "embedding cache is not working — which is the memory pressure that OOMKills "
    "this pod.",
    registry=REGISTRY,
)
S3_BYTES = Counter(
    "voxtell_s3_bytes_total",
    "Bytes moved to/from object storage, by direction.",
    ["direction"],
    registry=REGISTRY,
)
SCRATCH_FREE = Gauge(
    "voxtell_scratch_bytes_free",
    "Free bytes on the scratch volume. The emptyDir has a sizeLimit, and exceeding "
    "it evicts the pod — which is one of the ways the worker disappears.",
    registry=REGISTRY,
)
BUILD_INFO = Gauge(
    "voxtell_worker_build_info",
    "Always 1; labels carry the version and model directory.",
    ["version", "model"],
    registry=REGISTRY,
)


class _HeldTimer:
    """Keeps ``GPU_LOCK_HELD_CURRENT`` truthful without a background thread.

    prometheus_client evaluates a Gauge at scrape time only if you give it a callback,
    so the elapsed value is computed from a start timestamp on each scrape via
    ``set_function``. Ends up simpler than a ticker, and it cannot drift.
    """

    def __init__(self) -> None:
        self._acquired_at: float | None = None

    def acquired(self) -> None:
        self._acquired_at = time.monotonic()

    def released(self) -> None:
        if self._acquired_at is not None:
            GPU_LOCK_HELD.observe(time.monotonic() - self._acquired_at)
        self._acquired_at = None

    def elapsed(self) -> float:
        return 0.0 if self._acquired_at is None else time.monotonic() - self._acquired_at


HELD = _HeldTimer()
GPU_LOCK_HELD_CURRENT.set_function(HELD.elapsed)


def observe_scratch(path: str) -> None:
    """Record free space on the scratch volume. Cheap; call it per job."""
    try:
        import shutil

        SCRATCH_FREE.set(shutil.disk_usage(path).free)
    except Exception:  # pragma: no cover - a metric must not break a job
        pass


def serve(port: int, *, version: str, model: str) -> None:
    """Start the exposition server. Never fatal — a worker without metrics still works."""
    BUILD_INFO.labels(version=version, model=model).set(1)
    try:
        start_http_server(port, registry=REGISTRY)
        log.info("metrics on :%d", port)
    except Exception as exc:
        log.warning("could not start the metrics server on :%d: %s", port, exc)

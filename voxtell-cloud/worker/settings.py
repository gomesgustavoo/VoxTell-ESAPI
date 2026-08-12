"""Worker settings.

Shares DB_* / S3_* env names with the API so one ConfigMap + Secret pair serves
both Deployments. Worker-only knobs are WORKER_* / INFER_*. Deliberately does
not import the API's Settings class: the worker's env surface stays minimal (no
OIDC, no CORS, no presign endpoint — it never talks to a browser).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Database (sync psycopg; the worker is CPU/GPU-bound, not async) ---
    DB_HOST: str = "postgres.platform.svc.cluster.local"
    DB_PORT: int = 5432
    DB_NAME: str = "voxtell"
    DB_USER: str = "voxtell"
    DB_PASSWORD: str = ""

    # --- S3 (SeaweedFS) — internal endpoint only; the worker never presigns ---
    S3_ENDPOINT_INTERNAL: str = "http://seaweedfs.platform.svc.cluster.local:8333"
    S3_BUCKET: str = "voxtell"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "us-east-1"

    # --- Cross-service GPU mutex ---
    # Postgres advisory locks are per-database, and DicomSegVR's inference worker
    # lives in a different database, so the lock is taken on a third, empty
    # database that both connect to purely to serialise GPU access.
    # Empty string disables the mutex (single-tenant GPU, local dev).
    GPU_LOCK_DSN: str = ""
    GPU_LOCK_KEY: int = 0x76785F677075  # "vx_gpu"
    # Log a warning if we wait longer than this for the GPU (still keeps waiting).
    GPU_LOCK_WARN_SECONDS: float = 120.0
    # Hard ceiling on how long this worker may hold the mutex before it force-drops
    # it by terminating its own Postgres backend.
    #
    # This exists because the mutex is CROSS-PRODUCT: while we hold it, DicomSegVR's
    # inference pod blocks, in another namespace, for another product's users, with
    # nothing tying the two together in any dashboard. A job wedged inside the CUDA
    # call would otherwise hold it for the life of the process. 45 min is far above
    # any legitimate inference (measured 0.4-76 s) and below the point where someone
    # notices a second product is down.
    WORKER_GPU_LOCK_MAX_HOLD_SECONDS: float = 2700.0

    # --- Queue ---
    WORKER_POLL_SECONDS: float = 5.0
    # Renamed in spirit from HEARTBEAT: this now renews a *lease*, and only for jobs
    # that are demonstrably progressing. See worker/watchdog.py.
    WORKER_HEARTBEAT_SECONDS: float = 30.0
    # Retained only so a stale ConfigMap does not fail startup. The reclaim path is
    # driven by lease_expires_at now, not by heartbeat staleness.
    WORKER_HEARTBEAT_STALE_MINUTES: int = 20
    WORKER_MAX_ATTEMPTS: int = 3
    WORKER_SCRATCH_DIR: str = "/tmp/voxtell"
    WORKER_ALIVE_FILE: str = "/tmp/alive"
    # Fair-share: refuse to claim a second job for a user who already has one on
    # the GPU. A no-op with a single worker, correct if we ever add a second.
    WORKER_MAX_RUNNING_PER_USER: int = 1

    # --- Lease and deadline (see api/reclaim.py for the recovery side) ---
    # How long a claim is good for without renewal. Renewed only on observed
    # progress, so expiry means "dead or stalled" -> requeue.
    WORKER_LEASE_SECONDS: int = 600
    # Wall clock for one job, never extended. Expiry means "pathological" -> fail
    # terminally even with attempts left, because a job that wedges on one input
    # wedges again and each retry costs another hour of a shared GPU.
    WORKER_JOB_TIMEOUT_SECONDS: int = 3600
    # A job older than this (by queued_at) is promoted ahead of the priority bands.
    # Deliberately ranked ABOVE plan priority: unbounded starvation of a clinical
    # user is a worse outcome than a paying user waiting for one extra job. Flip the
    # first two ORDER BY clauses in _CLAIM_SQL to reverse that, and flip
    # tests/test_claim_fairness.py with it so the policy cannot drift silently.
    WORKER_QUEUE_AGING_SECONDS: int = 600
    # Stale-sweep throttle. This used to run on every poll iteration — twice per
    # ~5 s, i.e. ~34k pointless write transactions a day.
    WORKER_SWEEP_INTERVAL_SECONDS: float = 60.0

    # --- Stall detection (worker/watchdog.py) ---
    # Silence longer than this within a phase means the job is stalled: the lease is
    # NOT renewed and the liveness file is NOT touched.
    WORKER_STALL_GRACE_SECONDS: float = 300.0
    # Preprocess and postprocess are legitimately silent for longer — a single S3
    # download or a write_seg per prompt reports nothing while it runs.
    WORKER_STALL_GRACE_IO_SECONDS: float = 600.0
    # Blocking on pg_advisory_lock is legitimate non-progress, so waiting for the
    # GPU gets its own much larger budget. Without this, a long queue behind
    # DicomSegVR would look identical to a wedged job.
    WORKER_GPU_WAIT_MAX_SECONDS: float = 1800.0

    # --- Observability ---
    WORKER_METRICS_PORT: int = 9090

    # --- Inference ---
    # Model directory (plans.json + fold_0/checkpoint_final.pth). Empty means
    # "use $VOXTELL_MODEL, else download from Hugging Face".
    INFER_MODEL_DIR: str = "/models/voxtell_v1.1"
    INFER_DEVICE: str = "cuda"
    INFER_GPU_ID: int = 0
    # Precomputed text-embedding bank (.npz). Baked into the model store so the
    # pod needs no Hugging Face egress at runtime.
    INFER_EMBEDDING_BANK: str = "/models/text_embeddings.npz"
    # Overlap: CPU pre/post workers running alongside the one serialized GPU job.
    INFER_CPU_CONCURRENCY: int = 2
    # Warm the model at startup rather than on the first job (~30 s either way,
    # but startup is where the liveness probe already tolerates a long delay).
    INFER_WARM_ON_START: bool = True

    @property
    def device_str(self) -> str:
        return f"cuda:{self.INFER_GPU_ID}" if self.INFER_DEVICE == "cuda" else "cpu"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()


settings = get_settings()

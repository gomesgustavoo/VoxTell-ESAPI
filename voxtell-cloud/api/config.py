"""Settings, loaded from the environment (ConfigMap voxtell-config + Secret
voxtell-secrets in the cluster; a .env file locally).

Env names are unprefixed and match the DicomSegVR platform's naming contract for
the shared bits (DB_*, S3_*, OIDC_*) so a single ConfigMap idiom works across
both products. VoxTell-specific knobs are VOXTELL_*-prefixed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Database (Postgres via asyncpg) ---
    DB_HOST: str = "postgres.platform.svc.cluster.local"
    DB_PORT: int = 5432
    DB_NAME: str = "voxtell"
    DB_USER: str = "voxtell"
    DB_PASSWORD: str = ""

    # --- OIDC / Keycloak ---
    # iss is validated against OIDC_ISSUER (the public hostname, which is what
    # Keycloak stamps into the token). JWKS is fetched over cluster DNS because
    # the public hostname may not be routable from inside the cluster.
    OIDC_ISSUER: str = "https://auth.dicomsegvr.com/realms/dicomsegvr"
    OIDC_JWKS_URL: str = (
        "http://keycloak.platform.svc.cluster.local:8080"
        "/realms/dicomsegvr/protocol/openid-connect/certs"
    )
    OIDC_AUDIENCE: str = "voxtell-api"
    # Advertised to the ESAPI plugin by GET /v1/auth/config so neither the
    # device-code nor the loopback-redirect flow needs hard-coded URLs on the
    # client side. One client serves both grants.
    OIDC_DEVICE_CLIENT_ID: str = "voxtell-esapi"
    # Scopes the plugin requests. offline_access matters: the realm's access
    # tokens live 300 s and Eclipse launches the plugin fresh every run, so an
    # offline refresh token is what stops the planner re-authenticating each
    # time. It is part of the realm's default-roles-dicomsegvr composite.
    OIDC_PLUGIN_SCOPES: str = "openid profile email offline_access"
    # Loopback redirect ports for Authorization Code + PKCE. Fixed, not
    # ephemeral: Keycloak's redirect-URI wildcard is path-only, so every port
    # must be registered on the client verbatim. Three of them so a port already
    # taken on the workstation is not a dead end.
    OIDC_REDIRECT_PORTS: tuple[int, ...] = (47653, 47654, 47655)
    JWT_LEEWAY_SECONDS: int = 30

    # --- S3 (SeaweedFS) ---
    # Presigning MUST use the public endpoint: SigV4 signs the Host header, so a
    # URL signed for the internal name fails when the client hits the public one.
    S3_ENDPOINT_PUBLIC: str = "https://s3.dicomsegvr.com"
    S3_ENDPOINT_INTERNAL: str = "http://seaweedfs.platform.svc.cluster.local:8333"
    S3_BUCKET: str = "voxtell"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "us-east-1"  # SeaweedFS ignores it; SigV4 requires one.

    # --- CORS (the console SPA; the ESAPI plugin is not a browser) ---
    # Kept as a raw CSV string: pydantic-settings JSON-parses list-typed env
    # fields before any validator runs, which rejects plain comma-separated text.
    CORS_ORIGINS: str = "https://voxtell.dicomsegvr.com"

    # --- Upload / result lifecycle ---
    VOXTELL_PRESIGN_EXPIRY_SECONDS: int = 3600  # part PUTs; a slow link needs room
    VOXTELL_RESULT_EXPIRY_SECONDS: int = 900  # result GET redirect
    # Hard ceiling on a single upload. A 512x512x400 int16 volume is 200 MB raw
    # and compresses to roughly a quarter of that; 1 GiB is generous headroom.
    VOXTELL_MAX_UPLOAD_BYTES: int = 1024 * 1024 * 1024
    VOXTELL_MAX_VOXELS: int = 512 * 512 * 1024
    VOXTELL_MAX_PROMPTS: int = 16
    VOXTELL_PROMPT_MAX_CHARS: int = 200
    # Volumes and results are deleted this long after the job finishes. These are
    # patient images: keep the window short.
    VOXTELL_RESULT_TTL_HOURS: int = 24
    VOXTELL_SWEEP_INTERVAL_SECONDS: int = 900
    # A job stuck in awaiting_upload (client crashed mid-upload) is abandoned
    # after this long, and its multipart upload aborted. Also applies to a volume
    # left in `uploading`.
    VOXTELL_UPLOAD_TTL_MINUTES: int = 120

    # --- Reusable volumes (upload a series once, segment it many times) ---
    # Master switch. Default OFF so the API can ship the endpoints before the
    # worker rolls: an old worker unconditionally deletes its job's volume_key,
    # which would destroy a shared object after the first job and 404 the second.
    # /v1/me advertises the "volumes" capability from this flag, so a new plugin
    # against a not-yet-flipped API silently takes the legacy path. Flipping this
    # back is the rollback.
    VOXTELL_VOLUMES_ENABLED: bool = False
    # Idle expiry, slid forward on every use. Matches VOXTELL_UPLOAD_TTL_MINUTES
    # so an operator holds one number, and it survives a contouring session with
    # interruptions.
    VOXTELL_VOLUME_TTL_MINUTES: int = 120
    # Hard ceiling from created_at, never slid: a volume cannot outlive the shift
    # of the person who uploaded it. MUST stay <= VOXTELL_RESULT_TTL_HOURS so the
    # input CT never outlives the contours derived from it — the platform's
    # maximum patient-data retention is then unchanged by this feature.
    # tests/test_retention_policy.py enforces that inequality.
    VOXTELL_VOLUME_MAX_AGE_HOURS: int = 8
    # Live volumes (uploading + ready) one user may hold. One patient at a time,
    # plus room to compare a planning CT with a rescan, plus one in flight.
    # Exceeding it is a 409, not a 429: waiting does not help, releasing does.
    VOXTELL_MAX_VOLUMES_PER_USER: int = 3

    # --- Admission control (multi-user fairness on one GPU) ---
    VOXTELL_MAX_RUNNING_PER_USER: int = 1
    VOXTELL_MAX_QUEUED_PER_USER: int = 5
    # Concurrent awaiting_upload jobs per user. Bounds *storage*, not GPU: with
    # admit() moved to /submit, awaiting_upload no longer consumes a GPU slot, so
    # the thing left to limit is open multipart uploads. Keeping the two separate
    # is the actual bug fix — one counter used to do both jobs, so six failed
    # uploads denied GPU access for two hours.
    VOXTELL_MAX_AWAITING_UPLOAD_PER_USER: int = 3
    VOXTELL_DEFAULT_MONTHLY_QUOTA: int = 200
    # Poll cadence hint returned with every job status.
    VOXTELL_POLL_INTERVAL_SECONDS: int = 5

    # Ceiling on the whole queue, across all users. The per-user caps bound one
    # tenant; nothing bounded the total, so 20 users × 6 outstanding is 120 queued
    # jobs, and every volume-backed queued job pins its Volume alive against the
    # 50 Gi bucket. Exceeding this is a 429 `queue_full` (not a 503) so the ESAPI
    # client's existing Retry-After path handles it with no client change.
    #
    # Checked without a global lock, so it is approximate by ±(concurrent
    # submitters) — deliberately. A global advisory lock on the submit path would
    # serialise every tenant's submission for the sake of a soft limit.
    VOXTELL_MAX_GLOBAL_QUEUED: int = 40

    # --- Retry-After honesty ---
    # `Retry-After: 30` was hardcoded. With 20 jobs queued at ~60 s each the truth
    # is ~20 minutes, and the ESAPI client honours the header — so it re-POSTed
    # every 30 s, collected a 429 each time, and the planner concluded the service
    # was broken. The value is now derived from measured throughput; these two
    # bracket it.
    VOXTELL_MAX_RETRY_AFTER_SECONDS: int = 600
    # Fallback service time used until there is completion history to measure,
    # e.g. on a fresh deployment. Roughly a small CT end to end.
    VOXTELL_DEFAULT_JOB_SECONDS: float = 45.0
    # How long a measured service rate is reused before recomputing. The estimate
    # feeds a Retry-After, so being a minute stale costs nothing, while querying
    # per request would put a percentile scan on the submit path.
    VOXTELL_SERVICE_RATE_TTL_SECONDS: int = 60

    # --- Lease reclaim ---
    # How often the API looks for jobs whose worker died or stalled. Deliberately
    # far shorter than the retention sweep: recovery latency is user-visible, and
    # the query is a single indexed UPDATE on `jobs` with no object-storage calls.
    #
    # This lives in the API because the API is the thing that is always up. Stale
    # recovery used to run *only* inside the worker's own poll loop, which meant the
    # one failure it existed to handle — the worker not running — was the one it
    # could not handle.
    VOXTELL_RECLAIM_INTERVAL_SECONDS: int = 30
    # Deliberately the WORKER_-prefixed name: the API pod's envFrom already pulls
    # the whole voxtell-config ConfigMap, which carries WORKER_MAX_ATTEMPTS for the
    # worker. Declaring the same key here means the retry budget is one number in
    # one place that both sides read, instead of two that can silently disagree
    # about when a job should stop being retried.
    WORKER_MAX_ATTEMPTS: int = 3

    # --- Observability ---
    VOXTELL_METRICS_ENABLED: bool = True
    # Shared secret Prometheus presents to scrape /v1/metrics. Empty means the
    # endpoint refuses every request rather than serving unauthenticated: the
    # endpoint is internet-reachable (it lives under the /v1 path split), and queue
    # depth plus tenant counts should not be public. Comes from a Secret, not the
    # ConfigMap.
    VOXTELL_METRICS_TOKEN: str = ""
    # How often the DB-derived gauges are recomputed. Deliberately off the scrape
    # path — a slow Postgres must degrade the numbers' freshness, not time out the
    # scrape and blind the monitoring stack exactly when it is needed.
    VOXTELL_METRICS_REFRESH_SECONDS: int = 10

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

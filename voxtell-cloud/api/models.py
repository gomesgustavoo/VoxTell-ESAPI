"""SQLAlchemy 2.0 typed ORM models for the VoxTell-Cloud schema.

UUID primary keys are generated python-side so ``create_all`` works without the
pgcrypto extension. Timestamps are timezone-aware (TIMESTAMPTZ).

The worker reads and writes ``jobs`` with raw SQL (see ``worker/job.py``) rather
than the ORM — the queue semantics need ``FOR UPDATE SKIP LOCKED``, and the
worker image should not carry an async driver. These classes stay the single
source of truth for the schema; the worker's SQL must match them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


# Job states. A job walks awaiting_upload -> queued -> running -> done, with
# failed / cancelled as terminal alternatives and expired set by the sweeper
# once the result objects have been purged.
JOB_STATES = (
    "awaiting_upload",
    "queued",
    "running",
    "done",
    "failed",
    "cancelled",
    "expired",
)
TERMINAL_STATES = ("done", "failed", "cancelled", "expired")


class User(Base):
    """A Keycloak subject that has authenticated at least once.

    Rows are provisioned lazily on first-seen ``sub`` (see ``auth.py``); there is
    no separate sign-up. ``keycloak_sub`` is the only identity that matters —
    email/username are cached copies of token claims for display.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    keycloak_sub: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # NULL == unlimited. Enforced per calendar month (UTC) in quota.py.
    monthly_job_quota: Mapped[int | None] = mapped_column(
        Integer, nullable=True, server_default=text("200")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan")
    jobs: Mapped[list[Job]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    """A long-lived bearer credential for the ESAPI plugin.

    Only the SHA-256 hex of the token is stored, so a database leak does not
    yield usable keys and revocation is immediate (every request resolves the
    hash). ``prefix`` is the first characters of the plaintext, kept purely so
    the console can show the user which key is which.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")


VOLUME_STATES = ("uploading", "ready", "failed")


class Volume(Base):
    """An uploaded CT/MR that any number of jobs may segment.

    Exists so a planner uploads a series **once** and then iterates on prompts.
    Before this, every run re-read the image out of Eclipse, re-gzipped it and
    re-uploaded every part, so trying "liver" then "spleen" cost two 34 MB
    uploads over a hospital uplink.

    Two design points worth stating, because both are load-bearing:

    **The dedup key is (user, content, geometry), not content alone.** The blob
    is ``gzip(int16 (Z,Y,X))``, so identical bytes pin the voxel *count* but not
    the shape — ``(512,512,100)`` and ``(256,1024,100)`` are indistinguishable —
    and ``scaling_slope``/``scaling_intercept`` do not appear in the bytes at all.
    Reusing a volume under the wrong geometry puts contours in the wrong place in
    a patient. Including the geometry hash makes that structurally impossible;
    the cost is a redundant upload in a case that does not occur in practice.

    **Dedup is per-user.** Global dedup would be a cross-tenant read primitive:
    post a hash, learn from ``reused: true`` that the platform holds that series,
    then run jobs against it and receive contours for a patient you have no other
    access to. Hashes are guessable in exactly the case that matters — you
    already have the file. Scoping to the user also keeps every object under
    ``u/{user_id}/``, so erasure stays one ``delete_prefix`` call.

    Retention: see ``sweeper.py``. Lifetime is a sliding idle TTL under a hard
    age ceiling, never a refcount — the count of jobs that may still read a
    volume drops to zero the moment the last one finishes, which is precisely
    the behaviour this table exists to end.
    """

    __tablename__ = "volumes"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="uploading")

    # sha256 of the UNCOMPRESSED int16-LE (Z,Y,X) stream. Deliberately not of the
    # gzip output: gzip is not canonical, and the client already comments on
    # having *chosen* CompressionLevel.Fastest, so that knob is live and would
    # silently invalidate every cached volume if it ever changed.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # sha256 of the canonical geometry JSON — see the class docstring.
    geometry_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Geometry + affine_lps + content_sha256, copied onto each job so a job row
    # stays self-describing after the volume is gone.
    geometry: Mapped[dict] = mapped_column(JSONB, nullable=False)

    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    voxels: Mapped[int] = mapped_column(BigInteger, nullable=False)
    jobs_run: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Partial so a `failed` row does not block a re-upload of the same series.
        Index(
            "ux_volumes_dedup",
            "user_id",
            "content_sha256",
            "geometry_sha256",
            unique=True,
            postgresql_where=text("state <> 'failed'"),
        ),
        Index("ix_volumes_user_state", "user_id", "state"),
        Index("ix_volumes_expiry", "expires_at"),
    )


class Job(Base):
    """One segmentation request: upload -> queue -> GPU -> result."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="awaiting_upload")

    # Request
    prompts: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Full ESAPI geometry + the 4x4 LPS affine the API derived from it, so the
    # worker never recomputes it and a stored job is self-describing.
    geometry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    keep_largest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    want_mask: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # The volume this job segments, when it came from POST /v1/volumes. NULL for
    # a legacy inline-upload job, and that NULL is what the retention code
    # branches on: a legacy job still owns its volume and purges it eagerly, a
    # volume-backed job must not touch a shared object.
    #
    # SET NULL rather than CASCADE: releasing a volume must not delete the history
    # of jobs that ran against it. volume_key below stays the worker's
    # authoritative pointer either way.
    volume_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("volumes.id", ondelete="SET NULL"), nullable=True
    )

    # Object keys
    volume_key: Mapped[str] = mapped_column(Text, nullable=False)
    upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    mask_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Dispatch order. See worker/job.py::_CLAIM_SQL for how these combine.
    #
    # Higher wins. Stamped from the user's plan at enqueue and never recomputed, so
    # a mid-session plan change cannot reshuffle work that is already waiting —
    # predictable for the user, auditable for billing. 100 is the neutral value, so
    # a tier can move in either direction without negative numbers.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    # The user's own queue depth at the instant this job was enqueued: 0 for their
    # first waiting job, 1 for their second, and so on. Ordering by this *before*
    # the timestamp is what turns global FIFO into round-robin — user A's sixth job
    # sorts behind user B's first, so one tenant can no longer hold every position.
    #
    # It is a stored column rather than the obvious
    # ``row_number() OVER (PARTITION BY user_id ORDER BY queued_at)`` because
    # **Postgres rejects FOR UPDATE in any query containing a window function**, and
    # the claim cannot give up SKIP LOCKED. Computed under the same user-row lock
    # ``admit()`` already holds, so it cannot be gamed by concurrent submits.
    fair_rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Set when a transient failure is requeued with backoff; the claim skips rows
    # whose time has not come. NULL means eligible now.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- Liveness. Two clocks, deliberately, because they answer different
    # questions and want opposite treatment on expiry.
    #
    # "Is the worker still making progress on this?" Extended only by *observed
    # progress*, so a job wedged inside the GPU call stops renewing. Expiry means
    # the worker is dead or stalled -> requeue.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # "Has this job had long enough, whatever it is doing?" Set once at claim and
    # never extended. Expiry means the job is pathological -> fail terminally even
    # with attempts left, because a job that wedges on one input wedges again and
    # retrying spends another hour of the single shared GPU.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Progress / bookkeeping
    progress: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why this job failed, as a bounded label rather than free prose: 'transient',
    # 'permanent', 'stalled' (lease expired too many times), 'timeout' (deadline).
    # Recorded so the metric tells you when the classifier is guessing wrong.
    failure_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    bytes_in: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    voxels: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gpu_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True,
        server_default=text("now()"),
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="jobs")

    __table_args__ = (
        # Superseded by ix_jobs_dispatch below, kept because dropping an index is
        # not additive and a rollback to the previous image would want it back.
        Index(
            "ix_jobs_queued",
            "created_at",
            postgresql_where=text("state = 'queued'"),
        ),
        # The claim query's covering index, in the claim's own column order.
        # Partial on state='queued' so it stays tiny however many finished jobs
        # accumulate. The aging clause is a sort over the few hundred rows this
        # leaves; revisit only past ~5,000 queued.
        Index(
            "ix_jobs_dispatch",
            text("priority DESC"),
            "fair_rank",
            "queued_at",
            postgresql_where=text("state = 'queued'"),
        ),
        # The reclaim loop scans running jobs by lease, every 30 s.
        Index(
            "ix_jobs_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'running'"),
        ),
        # Backoff is rare, so keep this off the hot path with a doubly-partial index.
        Index(
            "ix_jobs_not_before",
            "not_before",
            postgresql_where=text("state = 'queued' AND not_before IS NOT NULL"),
        ),
        Index("ix_jobs_user_state", "user_id", "state"),
        # The retention interlock asks "does any queued/running job still need
        # this volume?" on every sweep.
        Index(
            "ix_jobs_volume_id",
            "volume_id",
            postgresql_where=text("volume_id IS NOT NULL"),
        ),
        # The stale sweep scans running jobs by heartbeat.
        Index(
            "ix_jobs_running_heartbeat",
            "heartbeat_at",
            postgresql_where=text("state = 'running'"),
        ),
    )


class UsageEvent(Base):
    """Append-only record of completed work — the basis for quota and billing.

    Separate from ``jobs`` so retention policy can purge job rows (and the PHI
    -adjacent geometry) without losing the usage history.
    """

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    prompts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    voxels: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gpu_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True,
        server_default=text("now()"),
    )

    __table_args__ = (Index("ix_usage_user_created", "user_id", "created_at"),)


class PromptEmbedding(Base):
    """Persistent cache of Qwen3 text embeddings, keyed by lowercased prompt.

    Upstream ships a precomputed bank for its known label set, and the predictor
    only loads the 8 GB text backbone for prompts outside it. This table extends
    that to *our* users' free text: a novel prompt costs one backbone load ever,
    across every worker restart.
    """

    __tablename__ = "prompt_embeddings"

    prompt: Mapped[str] = mapped_column(String(256), primary_key=True)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    # float16 little-endian, ``dim`` values — matching the dtype upstream uses for
    # both its published bank and its own writebacks. See worker/embeddings.py.
    vec: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # server_default, not just the Python-side default: this row is written by the
    # WORKER, in raw SQL (worker/embeddings.py::persist), which never goes through
    # the ORM — so a Python default never fires and the INSERT omits the column
    # entirely. Without the server default that is a NOT NULL violation on every
    # persist, which is exactly what happened: the cache silently stored nothing
    # and every novel prompt reloaded the 8 GB text backbone. Migration 0003
    # applies the same default to the already-created table.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )

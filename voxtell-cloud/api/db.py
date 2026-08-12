"""Async engine, session factory, and startup schema init.

``init_db()`` runs at lifespan startup. The API owns the schema; the worker never
migrates (deploy order: API first, worker second).

Concurrency is handled by a **session-level advisory lock**, not by hoping every
statement is idempotent. Only one replica migrates; the others block on the lock
and then find the work done. This replaced an earlier version that ran
``create_all`` and every migration inside a single ``engine.begin()`` and
swallowed failures with a ``log.warning``. That was worse than a race: Postgres
aborts the whole transaction on the first error, so every later statement failed
too **and the final COMMIT rolled back ``create_all`` itself** — leaving an API
with no tables at all, which still passed its readiness probe because
``/v1/health`` is 200 by design. Hence the three rules below:

1. One statement per transaction, so a failure cannot undo its predecessors.
2. Applied statements are recorded in ``schema_migrations`` and skipped, so
   ``_MIGRATIONS`` is free to eventually hold something non-idempotent.
3. **Fail loudly.** Only "already exists" SQLSTATEs are tolerated; anything else
   raises, and a CrashLoopBackOff is the correct outcome. Serving traffic against
   a half-migrated schema is not.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings
from .models import Base

log = logging.getLogger(__name__)

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    echo=False,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)

# Serialises schema init across replicas. ASCII "VXSCM". Must not collide with
# api/sweeper.py's _SWEEP_LOCK_KEY, api/reclaim.py's key, or worker/settings.py's
# GPU_LOCK_KEY — advisory locks share one namespace per database.
_SCHEMA_LOCK_KEY = 0x565853434D

# How long a replica waits for another replica's migration before giving up. A
# timeout raises, the pod restarts, and it tries again — which is what we want if
# the holder wedged. Generous enough for an index build on this data volume.
_SCHEMA_LOCK_TIMEOUT = "120s"

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    statement  TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# "This object already exists" — the only failures worth tolerating, and only
# because the live database predates schema_migrations, so the first run of this
# code re-executes statements that were already applied by hand or by the old
# loop. Everything else is a real bug and must surface.
#   42P07 duplicate_table   42710 duplicate_object   42701 duplicate_column
_BENIGN_SQLSTATES = frozenset({"42P07", "42710", "42701"})

# Additive migrations applied after create_all, as (id, statement) pairs. The id
# is what gets recorded, so it must be stable forever. Append only — never edit
# or renumber a shipped line, add a new one. Keeping every statement idempotent
# as well is still good practice, but is no longer what correctness rests on.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    # create_all creates new TABLES but never adds a column to an existing one,
    # so jobs.volume_id needs an explicit statement. The FK is safe because
    # create_all runs first, so `volumes` already exists. ADD COLUMN IF NOT
    # EXISTS skips the whole clause on a re-run, so the non-idempotent
    # REFERENCES never executes twice.
    (
        "0001_jobs_volume_id",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS volume_id UUID "
        "REFERENCES volumes(id) ON DELETE SET NULL",
    ),
    (
        "0002_ix_jobs_volume_id",
        "CREATE INDEX IF NOT EXISTS ix_jobs_volume_id ON jobs (volume_id) "
        "WHERE volume_id IS NOT NULL",
    ),
    # The worker persists prompt embeddings with raw SQL that omits created_at
    # (worker/embeddings.py::persist), so the column needs a DEFAULT at the
    # database level or every INSERT is a NOT NULL violation. It was — the cache
    # had never stored a single row while the on-disk bank held 14,194, so every
    # cache-miss prompt reloaded the ~8 GB Qwen3 text backbone. This repairs the
    # already-running worker with no worker rebuild; PromptEmbedding.created_at
    # carries the matching server_default so a fresh create_all agrees.
    (
        "0003_prompt_embeddings_created_at_default",
        "ALTER TABLE prompt_embeddings ALTER COLUMN created_at SET DEFAULT now()",
    ),
    # Same footgun as 0003, everywhere else it exists. Every NOT NULL created_at
    # carried only a Python-side ``default=utcnow``, which fires for ORM inserts
    # and not for raw SQL. Today the API writes all of these through the ORM so
    # nothing is broken — but the worker already speaks raw SQL to `jobs` and
    # `usage_events`, and the reclaim path adds more, so this is a landmine with a
    # known blast radius rather than a hypothetical one. Metadata-only ALTERs.
    (
        "0004_users_created_at_default",
        "ALTER TABLE users ALTER COLUMN created_at SET DEFAULT now()",
    ),
    (
        "0005_api_keys_created_at_default",
        "ALTER TABLE api_keys ALTER COLUMN created_at SET DEFAULT now()",
    ),
    (
        "0006_volumes_created_at_default",
        "ALTER TABLE volumes ALTER COLUMN created_at SET DEFAULT now()",
    ),
    (
        "0007_jobs_created_at_default",
        "ALTER TABLE jobs ALTER COLUMN created_at SET DEFAULT now()",
    ),
    (
        "0008_usage_events_created_at_default",
        "ALTER TABLE usage_events ALTER COLUMN created_at SET DEFAULT now()",
    ),
    # ---- Queue hardening. Every column has a DEFAULT or is nullable, so the
    # PREVIOUS worker image keeps claiming jobs happily against this schema. That
    # is what makes the API safe to roll out on its own, ahead of the worker.
    # ADD COLUMN with a non-volatile DEFAULT is metadata-only on PG11+.
    (
        "0009_jobs_priority",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 100",
    ),
    (
        "0010_jobs_fair_rank",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fair_rank INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "0011_jobs_not_before",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ",
    ),
    (
        "0012_jobs_lease_expires_at",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    ),
    (
        "0013_jobs_deadline_at",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ",
    ),
    (
        "0014_jobs_failure_class",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failure_class VARCHAR(16)",
    ),
    (
        "0015_ix_jobs_dispatch",
        "CREATE INDEX IF NOT EXISTS ix_jobs_dispatch ON jobs "
        "(priority DESC, fair_rank, queued_at) WHERE state = 'queued'",
    ),
    (
        "0016_ix_jobs_lease",
        "CREATE INDEX IF NOT EXISTS ix_jobs_lease ON jobs (lease_expires_at) "
        "WHERE state = 'running'",
    ),
    (
        "0017_ix_jobs_not_before",
        "CREATE INDEX IF NOT EXISTS ix_jobs_not_before ON jobs (not_before) "
        "WHERE state = 'queued' AND not_before IS NOT NULL",
    ),
    # State is a plain VARCHAR(24) with the allowed values living only in a Python
    # tuple, so a typo in any of the several raw-SQL writers would persist happily
    # and then never be claimed, expired or listed again. CHECK rather than an
    # ENUM: enum values cannot be removed or reordered, and ALTER TYPE ADD VALUE
    # interacts badly with a transactional migration runner.
    #
    # ADD CONSTRAINT has no IF NOT EXISTS, hence the pg_constraint guard. NOT VALID
    # then VALIDATE keeps the lock brief and tolerates pre-existing rows; there are
    # none out of range today, so VALIDATE succeeds immediately.
    (
        "0018_ck_jobs_state",
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_jobs_state') THEN
                ALTER TABLE jobs ADD CONSTRAINT ck_jobs_state CHECK (state IN (
                    'awaiting_upload','queued','running','done','failed','cancelled','expired'
                )) NOT VALID;
                ALTER TABLE jobs VALIDATE CONSTRAINT ck_jobs_state;
            END IF;
        END $$
        """,
    ),
    (
        "0019_ck_volumes_state",
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_volumes_state') THEN
                ALTER TABLE volumes ADD CONSTRAINT ck_volumes_state CHECK (state IN (
                    'uploading','ready','failed'
                )) NOT VALID;
                ALTER TABLE volumes VALIDATE CONSTRAINT ck_volumes_state;
            END IF;
        END $$
        """,
    ),
    (
        "0020_ck_jobs_failure_class",
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname = 'ck_jobs_failure_class') THEN
                ALTER TABLE jobs ADD CONSTRAINT ck_jobs_failure_class CHECK (
                    failure_class IS NULL OR failure_class IN (
                        'transient','permanent','stalled','timeout'
                    )
                ) NOT VALID;
                ALTER TABLE jobs VALIDATE CONSTRAINT ck_jobs_failure_class;
            END IF;
        END $$
        """,
    ),
    # Every running job must carry a lease. This makes it *structurally impossible* to
    # ship a claim path that forgets to set one — which would otherwise be a silent
    # regression to the old behaviour, where a job had no expiry and a wedged worker
    # held the cross-product GPU mutex forever with nothing able to reclaim it.
    #
    # ORDERING IS LOAD-BEARING: this must only reach a database whose worker already
    # sets lease_expires_at at claim time. The pre-0.1.3 worker did not, so an API image
    # carrying this migration deployed ahead of the worker would make **every claim
    # fail**. NOT VALID + VALIDATE also means the VALIDATE step fails outright if any
    # running row currently lacks a lease, so the deploy is loud rather than subtly
    # broken. Hence: worker 0.1.3 first, then the API image containing this line.
    (
        "0021_ck_jobs_running_has_lease",
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname = 'ck_jobs_running_has_lease') THEN
                ALTER TABLE jobs ADD CONSTRAINT ck_jobs_running_has_lease CHECK (
                    state <> 'running' OR lease_expires_at IS NOT NULL
                ) NOT VALID;
                ALTER TABLE jobs VALIDATE CONSTRAINT ck_jobs_running_has_lease;
            END IF;
        END $$
        """,
    ),
)


def _sqlstate(exc: BaseException) -> str | None:
    """The Postgres SQLSTATE behind a SQLAlchemy wrapper, driver-agnostically.

    asyncpg and psycopg3 both expose ``sqlstate``; psycopg2 calls it ``pgcode``.
    Matching on SQLSTATE rather than on driver exception classes is what lets this
    module work under both the API's asyncpg engine and the worker's psycopg one.
    """
    orig = getattr(exc, "orig", None)
    for attr in ("sqlstate", "pgcode"):
        code = getattr(orig, attr, None)
        if code:
            return str(code)
    return None


async def _record_migration(mid: str, stmt: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO schema_migrations (id, statement) VALUES (:id, :stmt) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": mid, "stmt": stmt},
        )


async def _apply_schema() -> None:
    """Create tables and apply pending migrations. Caller holds the schema lock."""
    # The bookkeeping table first, before anything consults it.
    async with engine.begin() as conn:
        await conn.execute(text(_SCHEMA_MIGRATIONS_DDL))

    # create_all in its own transaction, so no later failure can roll it back.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id FROM schema_migrations"))
        applied = {row[0] for row in result}

    for mid, stmt in _MIGRATIONS:
        if mid in applied:
            continue
        try:
            # DDL and its record commit together, so a rolled-back statement is
            # never marked applied.
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
                await conn.execute(
                    text(
                        "INSERT INTO schema_migrations (id, statement) "
                        "VALUES (:id, :stmt) ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": mid, "stmt": stmt},
                )
        except (ProgrammingError, OperationalError, DBAPIError) as exc:
            state = _sqlstate(exc)
            if state not in _BENIGN_SQLSTATES:
                log.error(
                    "migration %s FAILED (SQLSTATE %s): %s", mid, state, stmt, exc_info=exc
                )
                # Imported lazily: api.metrics imports this module for the engine and
                # the session factory, so a top-level import here is a cycle.
                try:
                    from .metrics import SCHEMA_MIGRATION_FAILURES

                    SCHEMA_MIGRATION_FAILURES.inc()
                except Exception:  # pragma: no cover - never mask the real failure
                    pass
                raise
            # Already present. The transaction above is aborted, so the record
            # needs its own.
            log.info("migration %s already applied (SQLSTATE %s)", mid, state)
            await _record_migration(mid, stmt)
        else:
            log.info("migration %s applied", mid)


async def init_db() -> None:
    async with engine.connect() as lock_conn:
        # lock_timeout applies to advisory locks, so a wedged holder surfaces as a
        # raise rather than a lifespan that never returns.
        await lock_conn.execute(text(f"SET LOCAL lock_timeout = '{_SCHEMA_LOCK_TIMEOUT}'"))
        await lock_conn.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": _SCHEMA_LOCK_KEY}
        )
        # Session-level locks survive COMMIT; committing here just avoids holding
        # an idle-in-transaction connection for the whole migration.
        await lock_conn.commit()
        try:
            await _apply_schema()
        finally:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _SCHEMA_LOCK_KEY}
            )
            await lock_conn.commit()


async def ping() -> bool:
    """Cheap liveness check used by /v1/health."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request.

    The commit here is a **safety net, not the primary commit**. FastAPI runs the
    teardown of a ``yield`` dependency *after* the response has been sent, so a
    route that relies on it returns an id the client can use before the row is
    visible to anyone else. That race is real and easy to hit: create a job, then
    immediately POST to /submit, and the second request (often on the other
    replica) 404s.

    Every mutating route therefore calls ``session.commit()`` itself before
    returning. This teardown then covers incidental writes — a provisioned user
    row on a read-only route, a throttled ``last_used_at`` bump — where nothing
    reads back immediately.
    """
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

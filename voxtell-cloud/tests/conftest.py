"""Postgres-backed test fixtures for the queue, admission control and migrations.

Everything here is gated on ``VOXTELL_TEST_DATABASE_URL``. When it is unset the
``pg`` fixtures skip, so ``pytest`` with no database still runs the pure-unit suite
that predates this file. **``pytest -m "not pg"`` must never need a database** — the
older tests deliberately have no DB dependency and must not acquire one.

Why a real Postgres and not a fake
----------------------------------
The behaviour under test *is* Postgres behaviour: ``FOR UPDATE SKIP LOCKED``,
session-level advisory locks, ``make_interval``, partial-index eligibility, and the
fact that Postgres **rejects ``FOR UPDATE`` in any query containing a window
function** (which is why the dispatch rank is a stored column rather than a
``row_number() OVER (PARTITION BY user_id)``). None of that survives a mock, and a
different Postgres version could differ.

Why the cluster's own Postgres
------------------------------
``testcontainers`` needs the Docker socket, which is root-only on this box, and
``docker-compose.yml``'s postgres service has the same problem plus it publishes no
host port. The cluster Postgres is already running, costs nothing extra, and is the
exact version production runs.

Setup, once (the ``voxtell`` role has **no CREATEDB**, so this must run as postgres)::

    export KUBECONFIG=/home/tavulha/.kube/config
    kubectl -n platform exec postgres-0 -- \\
        psql -U postgres -c 'CREATE DATABASE voxtell_test OWNER voxtell'

Then, per shell::

    export VOXTELL_TEST_DATABASE_URL="postgresql://voxtell:$(kubectl -n voxtell get \\
        secret voxtell-secrets -o jsonpath='{.data.DB_PASSWORD}' | base64 -d)@10.42.0.193:5432/voxtell_test"
    pytest -m pg

Every session gets its own ``test_<hex>`` **schema** inside that database, dropped at
the end. That is what makes it safe: unqualified table names in the worker's raw SQL
resolve through ``search_path``, parallel runs never collide, and no test can reach a
production row even if the DSN is pointed at the wrong database by accident.

Time is expressed in SQL, never in Python
-----------------------------------------
Every reclaim, aging and staleness predicate is evaluated by Postgres via ``now()``.
Tests therefore *write* timestamps (``now() - interval '11 minutes'``) instead of
patching a Python clock. Do not reach for ``freezegun``; it cannot move the database's
clock and would silently prove nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_ENV_VAR = "VOXTELL_TEST_DATABASE_URL"

# Every test connection fails a lock wait after 5 s instead of waiting forever.
#
# This is not tuning, it is a debugging tool. The code under test takes real row
# locks (`admit()` holds SELECT ... FOR UPDATE on the user row) and real advisory
# locks, and a test that accidentally holds one across another connection's write
# will deadlock. Without a timeout that presents as pytest hanging with no output,
# which tells you nothing; with one you get an immediate `lock_not_available`
# naming the statement. Concretely: an uncommitted `admit()` blocks any INSERT into
# `jobs` for that user from another connection, because the FK takes FOR KEY SHARE
# on the parent row.
_LOCK_TIMEOUT_MS = "5000"
_LOCK_TIMEOUT_OPT = f"-clock_timeout={_LOCK_TIMEOUT_MS}"
_SKIP = (
    f"{_ENV_VAR} is not set — see tests/conftest.py for the one-line setup. "
    'Run `pytest -m "not pg"` to skip these deliberately.'
)


# --------------------------------------------------------------------------- DSNs


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """Bare DSN from the environment, or skip the whole DB suite."""
    dsn = os.environ.get(_ENV_VAR, "").strip()
    if not dsn:
        pytest.skip(_SKIP, allow_module_level=True)
    return dsn


@pytest.fixture(scope="session")
def pg_schema(pg_dsn: str) -> Iterator[str]:
    """A private schema for this session, dropped on the way out."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_sync_url(pg_dsn), poolclass=None, future=True)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield schema
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _sync_url(dsn: str) -> str:
    """Force the psycopg driver — the queue SQL under test is the worker's."""
    if dsn.startswith("postgresql+"):
        return dsn
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1)


def _async_url(dsn: str) -> str:
    if dsn.startswith("postgresql+"):
        return dsn
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


# ------------------------------------------------------------------------ engines


@pytest.fixture(scope="session")
def sync_engine(pg_dsn: str, pg_schema: str) -> Iterator[Engine]:
    """psycopg engine pinned to the test schema — what the worker's SQL runs on.

    ``search_path`` goes in via libpq's ``options``, so unqualified names in
    ``worker/job.py``'s raw SQL resolve here with no rewriting of that SQL.
    ``pool_size`` is above the default because the concurrency tests deliberately
    run several simultaneous ``claim_next`` calls.
    """
    engine = create_engine(
        _sync_url(pg_dsn),
        connect_args={"options": f"-csearch_path={pg_schema} {_LOCK_TIMEOUT_OPT}"},
        pool_size=12,
        max_overflow=8,
        pool_pre_ping=True,
        future=True,
    )
    yield engine
    engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def async_engine(pg_dsn: str, pg_schema: str) -> AsyncIterator[AsyncEngine]:
    """asyncpg engine pinned to the test schema — what the API's code runs on.

    asyncpg has no libpq ``options``; it takes ``server_settings`` instead.
    """
    engine = create_async_engine(
        _async_url(pg_dsn),
        connect_args={
            "server_settings": {"search_path": pg_schema, "lock_timeout": _LOCK_TIMEOUT_MS}
        },
        pool_size=8,
        max_overflow=4,
        pool_pre_ping=True,
    )
    yield engine
    await engine.dispose()


# ------------------------------------------------------------------------- schema


@pytest_asyncio.fixture(scope="session")
async def schema_ready(async_engine: AsyncEngine, pg_schema: str) -> AsyncIterator[None]:
    """Run the real ``init_db()`` into the test schema.

    Deliberately the production code path rather than a bare ``create_all``, so the
    fixture itself exercises the migration runner every session.
    """
    from api import db as api_db

    real_engine, real_sessionmaker = api_db.engine, api_db.SessionLocal
    api_db.engine = async_engine
    api_db.SessionLocal = async_sessionmaker(
        bind=async_engine, expire_on_commit=False, autoflush=False
    )
    try:
        await api_db.init_db()
        yield
    finally:
        api_db.engine, api_db.SessionLocal = real_engine, real_sessionmaker


@pytest.fixture
def worker_db(sync_engine: Engine, schema_ready: None, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Point the worker's queue code at the test schema.

    One patch target only: ``worker/job.py`` and ``worker/embeddings.py`` both go
    through ``db.get_engine()`` on the module rather than importing the name, so
    patching ``worker.db.get_engine`` reaches all of them. Keep it that way.
    """
    from worker import db as worker_db_mod

    monkeypatch.setattr(worker_db_mod, "get_engine", lambda: sync_engine)
    return sync_engine


@pytest_asyncio.fixture
async def db_session(async_engine: AsyncEngine, schema_ready: None) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(bind=async_engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        yield session
        await session.rollback()


@pytest.fixture(autouse=True)
def _clean_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    """Truncate between DB tests so ordering never matters.

    Only engages for tests that actually took a DB fixture — the pure-unit suite is
    untouched. ``users`` cascades to jobs/volumes/api_keys/usage_events via their
    FKs; ``prompt_embeddings`` has no FK so it is named explicitly.
    """
    yield
    if "sync_engine" not in request.fixturenames:
        return
    engine: Engine = request.getfixturevalue("sync_engine")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users, prompt_embeddings CASCADE"))


# ----------------------------------------------------------------------- factories


@pytest.fixture
def make_user(sync_engine: Engine):
    """Insert a user and return its id. ``monthly_job_quota=None`` means unlimited."""

    def _make(
        *,
        email: str | None = None,
        monthly_job_quota: int | None = 200,
        keycloak_sub: str | None = None,
    ) -> uuid.UUID:
        uid = uuid.uuid4()
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, keycloak_sub, email, username, monthly_job_quota) "
                    "VALUES (:id, :sub, :email, :username, :quota)"
                ),
                {
                    "id": uid,
                    "sub": keycloak_sub or f"sub-{uid.hex[:12]}",
                    "email": email or f"{uid.hex[:8]}@test.invalid",
                    "username": uid.hex[:8],
                    "quota": monthly_job_quota,
                },
            )
        return uid

    return _make


@pytest.fixture
def make_job(sync_engine: Engine):
    """Insert a job directly, bypassing the API.

    ``queued_at`` / ``created_at`` accept a **SQL expression string** so tests can
    place a job in the past (``"now() - interval '11 minutes'"``) without a Python
    clock. Columns added by later phases (``fair_rank``, ``priority``, leases) are
    passed through ``extra`` so this factory does not need editing every phase.
    """

    def _make(
        user_id: uuid.UUID,
        *,
        state: str = "queued",
        created_at: str = "now()",
        queued_at: str | None = "now()",
        prompts: list[str] | None = None,
        attempts: int = 0,
        volume_key: str = "u/test/jobs/test/volume.bin.gz",
        **extra: Any,
    ) -> uuid.UUID:
        import json

        jid = uuid.uuid4()
        cols = ["id", "user_id", "state", "prompts", "geometry", "volume_key", "attempts"]
        vals = [":id", ":user_id", ":state", "CAST(:prompts AS JSONB)", "'{}'::jsonb",
                ":volume_key", ":attempts"]
        params: dict[str, Any] = {
            "id": jid,
            "user_id": user_id,
            "state": state,
            "prompts": json.dumps(prompts if prompts is not None else ["liver"]),
            "volume_key": volume_key,
            "attempts": attempts,
        }
        # SQL expressions, inlined rather than bound, so `now() - interval ...` works.
        cols.append("created_at")
        vals.append(created_at)
        if queued_at is not None:
            cols.append("queued_at")
            vals.append(queued_at)

        # A `running` row must carry a lease — ck_jobs_running_has_lease enforces it,
        # so that a claim path which forgets to set one cannot be shipped. The factory
        # therefore has to do what the real claim does. Defaults only: a test that wants
        # a lapsed lease passes its own via `extra`, and one that wants to reproduce a
        # pre-lease row (claimed by an older worker image) passes an explicit None.
        if state == "running":
            if "lease_expires_at" not in extra:
                cols.append("lease_expires_at")
                vals.append("now() + interval '10 minutes'")
            if "deadline_at" not in extra:
                cols.append("deadline_at")
                vals.append("now() + interval '1 hour'")

        for key, value in extra.items():
            cols.append(key)
            vals.append(f":{key}")
            params[key] = value

        with sync_engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
                params,
            )
        return jid

    return _make


@pytest.fixture
def make_volume(sync_engine: Engine):
    def _make(
        user_id: uuid.UUID,
        *,
        state: str = "ready",
        content_sha256: str | None = None,
        geometry_sha256: str | None = None,
        expires_at: str = "now() + interval '2 hours'",
        **extra: Any,
    ) -> uuid.UUID:
        vid = uuid.uuid4()
        params: dict[str, Any] = {
            "id": vid,
            "user_id": user_id,
            "state": state,
            "csha": content_sha256 or uuid.uuid4().hex * 2,
            "gsha": geometry_sha256 or uuid.uuid4().hex * 2,
            "key": f"u/{user_id}/volumes/{vid.hex}.bin.gz",
        }
        cols = ["id", "user_id", "state", "content_sha256", "geometry_sha256", "geometry",
                "object_key", "expires_at"]
        vals = [":id", ":user_id", ":state", ":csha", ":gsha", "'{}'::jsonb", ":key", expires_at]
        for key, value in extra.items():
            cols.append(key)
            vals.append(f":{key}")
            params[key] = value
        with sync_engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO volumes ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
                params,
            )
        return vid

    return _make


@pytest.fixture
def job_state(sync_engine: Engine):
    """Read back one job's columns — the assertion helper for queue tests."""

    def _read(job_id: uuid.UUID, *columns: str) -> dict[str, Any]:
        cols = ", ".join(columns) if columns else "*"
        with sync_engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {cols} FROM jobs WHERE id = :jid"), {"jid": job_id}
            ).mappings().first()
        assert row is not None, f"job {job_id} not found"
        return dict(row)

    return _read

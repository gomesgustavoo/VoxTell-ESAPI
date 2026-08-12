"""The schema-init contract.

Two production incidents live in this file.

The first is the one that motivated the ``init_db()`` rewrite. The old version ran
``create_all`` **and** every migration inside a single ``engine.begin()`` and caught
failures with a ``log.warning``. Postgres aborts the entire transaction on the first
error, so every later statement failed too and the final COMMIT rolled back
``create_all`` itself — an API with no tables at all, which still passed readiness
because ``/v1/health`` returns 200 by design. ``test_failed_migration_*`` are the
regression tests.

The second is the embedding cache that had never stored a single row.
``worker/embeddings.py::persist`` INSERTs ``(prompt, dim, vec)`` in raw SQL, and
``prompt_embeddings.created_at`` was ``NOT NULL`` with no server default — the model
carried only a Python-side ``default=utcnow``, which never fires for raw SQL. Every
persist raised ``NotNullViolation``, so a novel prompt reloaded the ~8 GB Qwen3 text
backbone on every job forever. ``test_worker_can_persist_an_embedding`` pins it by
calling the real worker function, so the test cannot drift from the call site.
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from api import db as api_db
from api import models

pytestmark = pytest.mark.pg


async def _tables(engine: AsyncEngine, schema: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}
        )
        return {row[0] for row in result}


async def _ledger(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id FROM schema_migrations ORDER BY id"))
        return [row[0] for row in result]


async def test_create_all_produced_every_model_table(
    async_engine: AsyncEngine, pg_schema: str, schema_ready: None
) -> None:
    present = await _tables(async_engine, pg_schema)
    expected = {t.name for t in models.Base.metadata.sorted_tables}
    assert not expected - present, f"init_db did not create {expected - present}"
    # The ledger is raw DDL, deliberately outside Base.metadata.
    assert "schema_migrations" in present


async def test_every_migration_is_recorded_exactly_once(
    async_engine: AsyncEngine, schema_ready: None
) -> None:
    ledger = await _ledger(async_engine)
    assert ledger == [mid for mid, _ in api_db._MIGRATIONS]
    assert len(ledger) == len(set(ledger)), "duplicate rows in schema_migrations"


async def test_init_db_is_idempotent(async_engine: AsyncEngine, schema_ready: None) -> None:
    """A second run must be a clean no-op, not a pile of tolerated errors."""
    before = await _ledger(async_engine)
    await api_db.init_db()
    await api_db.init_db()
    assert await _ledger(async_engine) == before


async def test_migration_ids_are_unique_and_stable() -> None:
    """The id is what gets recorded, so a collision would silently skip a statement."""
    ids = [mid for mid, _ in api_db._MIGRATIONS]
    assert len(ids) == len(set(ids)), f"duplicate migration id in _MIGRATIONS: {ids}"
    assert ids == sorted(ids), "migrations must be appended in order, never renumbered"


async def test_failed_migration_raises_instead_of_being_swallowed(
    async_engine: AsyncEngine, schema_ready: None
) -> None:
    original = api_db._MIGRATIONS
    api_db._MIGRATIONS = original + (
        ("9999_broken", "ALTER TABLE jobs ADD COLUMN bad_col NO_SUCH_TYPE"),
    )
    try:
        with pytest.raises(Exception) as caught:
            await api_db.init_db()
    finally:
        api_db._MIGRATIONS = original

    # Not swallowed, and the SQLSTATE survives so the log line is diagnosable.
    assert api_db._sqlstate(caught.value) not in api_db._BENIGN_SQLSTATES


async def test_failed_migration_does_not_roll_back_create_all(
    async_engine: AsyncEngine, pg_schema: str, schema_ready: None
) -> None:
    """The exact old bug: one bad statement must not take the tables with it."""
    expected = {t.name for t in models.Base.metadata.sorted_tables}
    original = api_db._MIGRATIONS
    api_db._MIGRATIONS = original + (
        ("9998_broken", "CREATE INDEX ix_nope ON no_such_table (nope)"),
    )
    try:
        with pytest.raises(Exception):
            await api_db.init_db()
    finally:
        api_db._MIGRATIONS = original

    present = await _tables(async_engine, pg_schema)
    assert not expected - present, "create_all was rolled back by a later failure"


async def test_failed_migration_is_not_marked_applied(
    async_engine: AsyncEngine, schema_ready: None
) -> None:
    original = api_db._MIGRATIONS
    api_db._MIGRATIONS = original + (
        ("9997_broken", "ALTER TABLE jobs ADD COLUMN worse_col NO_SUCH_TYPE"),
    )
    try:
        with pytest.raises(Exception):
            await api_db.init_db()
    finally:
        api_db._MIGRATIONS = original

    assert "9997_broken" not in await _ledger(async_engine), (
        "a statement that did not apply must not be recorded, or it can never be retried"
    )


async def test_already_applied_migration_is_tolerated_and_recorded(
    async_engine: AsyncEngine, schema_ready: None
) -> None:
    """The live-upgrade path: a DB that predates the ledger.

    Production had every migration applied by hand with no ``schema_migrations``
    table. On the first run of the new code those statements re-execute; the ones
    whose objects already exist must be tolerated *and* recorded, or they would be
    retried on every single pod start forever.
    """
    original = api_db._MIGRATIONS
    # Not idempotent on purpose — no IF NOT EXISTS — so it must fail the second time
    # with a benign SQLSTATE rather than crash the API.
    api_db._MIGRATIONS = original + (
        ("9990_dup", "CREATE TABLE dup_probe (id INT PRIMARY KEY)"),
        ("9991_dup_again", "CREATE TABLE dup_probe (id INT PRIMARY KEY)"),
    )
    try:
        await api_db.init_db()  # must not raise
        ledger = await _ledger(async_engine)
        assert "9990_dup" in ledger
        assert "9991_dup_again" in ledger, "a benign duplicate must still be recorded"
    finally:
        api_db._MIGRATIONS = original
        async with async_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS dup_probe"))
            await conn.execute(
                text("DELETE FROM schema_migrations WHERE id IN ('9990_dup','9991_dup_again')")
            )


def test_benign_sqlstates_are_only_already_exists() -> None:
    """Widening this set is how a real bug becomes invisible again."""
    assert api_db._BENIGN_SQLSTATES == frozenset({"42P07", "42710", "42701"})


def test_schema_lock_key_does_not_collide() -> None:
    """Advisory locks share one namespace per database."""
    from api import sweeper
    from worker import settings as worker_settings

    keys = {
        "schema": api_db._SCHEMA_LOCK_KEY,
        "sweep": sweeper._SWEEP_LOCK_KEY,
        "gpu": worker_settings.settings.GPU_LOCK_KEY,
    }
    assert len(set(keys.values())) == len(keys), f"advisory key collision: {keys}"


# --------------------------------------------------------------- the embedding bug


def test_prompt_embeddings_created_at_has_a_server_default(sync_engine: Engine,
                                                           schema_ready: None) -> None:
    with sync_engine.connect() as conn:
        default = conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'prompt_embeddings' AND column_name = 'created_at'"
            )
        ).scalar()
    assert default and "now()" in default, (
        "created_at needs a DATABASE default: the worker writes this row in raw SQL, "
        "so a Python-side default never fires and the INSERT omits the column"
    )


def test_worker_can_persist_an_embedding(worker_db: Engine) -> None:
    """Calls the real worker function, so this cannot drift from the call site.

    Before migration 0003 this raised NotNullViolation on every invocation and the
    cache silently stored nothing.
    """
    from worker import embeddings

    vec = np.arange(8, dtype=np.float16)
    assert embeddings.persist({"cauda equina": vec}) == 1

    loaded = embeddings.load_all()
    assert "cauda equina" in loaded
    # float16 round-trips byte-exactly; a float32 read would corrupt every value.
    np.testing.assert_array_equal(loaded["cauda equina"], vec)


def test_embedding_persist_is_idempotent(worker_db: Engine) -> None:
    """ON CONFLICT DO NOTHING: re-persisting a known prompt must not raise."""
    from worker import embeddings

    vec = np.ones(4, dtype=np.float16)
    embeddings.persist({"liver": vec})
    embeddings.persist({"liver": vec})
    assert len(embeddings.load_all()) == 1


def test_embedding_dtype_matches_the_model_comment(worker_db: Engine) -> None:
    """The model said float32 while the worker wrote float16 — a reader-side landmine.

    Pinning the actual dtype means the next person to touch either side has to keep
    them agreeing.
    """
    from worker import embeddings

    assert embeddings._DTYPE is np.float16
    embeddings.persist({"brainstem": np.zeros(6, dtype=np.float16)})
    with worker_db.connect() as conn:
        dim, nbytes = conn.execute(
            text("SELECT dim, octet_length(vec) FROM prompt_embeddings WHERE prompt='brainstem'")
        ).first()
    assert nbytes == dim * 2, "stored vector is not 2 bytes per value, i.e. not float16"

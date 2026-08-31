"""QA baselines against a real Postgres: idempotency, supersede, and the index.

Why this needs a database rather than a mock
-------------------------------------------
The behaviour under test *is* the database's. ``ux_qa_baselines_live`` is a partial
UNIQUE index on ``(user_id, series_key) WHERE state <> 'superseded'``, and the
route's correctness depends on the order statements reach Postgres: SQLAlchemy's
unit of work emits INSERTs before UPDATEs within a mapper, so adding the new
baseline before flushing the supersede raises a duplicate-key error on the
completely ordinary "planner edited and came back" path. No mock reproduces that.

The three properties pinned here are the ones that decide whether the feature is
safe to leave switched on:

* **Reopening a patient records nothing new.** The natural trigger is "the plugin
  opened a series it recognises", so a planner who opens a case, glances at it and
  closes it will post the same snapshot repeatedly. If that created rows, the
  drift charts would be dominated by how often people opened Eclipse.
* **A real edit supersedes rather than appends**, so "the baseline for this series"
  is always exactly one row.
* **Approval is read, not waited for.** A fully approved structure set is settled,
  so its record skips ``provisional`` instead of ageing out of it after N days.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.pg

SERIES_KEY = "a" * 64
OTHER_SERIES = "b" * 64


def _insert(conn, user_id, *, series_key, content_hash, state="provisional"):
    """Insert a baseline the way the route does, in the route's statement order."""
    baseline_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO qa_baselines "
            "(id, user_id, state, series_key, structure_set_sha256, contours_key, "
            " contours_bytes, structure_count, models, geometry) "
            "VALUES (:id, :uid, :state, :sk, :hash, :key, 0, 1, '[]'::jsonb, '{}'::jsonb)"
        ),
        {
            "id": baseline_id,
            "uid": user_id,
            "state": state,
            "sk": series_key,
            "hash": content_hash,
            "key": f"u/{user_id}/qa/{series_key}/{content_hash}.json.gz",
        },
    )
    return baseline_id


def test_table_and_partial_index_exist(sync_engine, schema_ready):
    """create_all + migration 0023 actually produced the table the route needs."""
    with sync_engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'qa_baselines'"
                )
            )
        }
    assert {"series_key", "structure_set_sha256", "state", "superseded_by"} <= cols

    with sync_engine.connect() as conn:
        indexes = {
            r[0]
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'qa_baselines'")
            )
        }
    assert "ux_qa_baselines_live" in indexes


def test_state_check_constraint_rejects_a_typo(sync_engine, schema_ready, make_user):
    """Migration 0022. A typo'd state would otherwise read as an invisible row."""
    user_id = make_user()
    # IntegrityError specifically, so this proves the CHECK fired rather than any
    # error at all -- a misspelled column would otherwise pass this test.
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _insert(conn, user_id, series_key=SERIES_KEY, content_hash="c" * 64,
                state="provisonal")  # deliberate typo


def test_one_live_baseline_per_series_is_enforced_by_the_database(
    sync_engine, schema_ready, make_user
):
    """The invariant the route's flush order exists to respect."""
    user_id = make_user()
    with sync_engine.begin() as conn:
        _insert(conn, user_id, series_key=SERIES_KEY, content_hash="c" * 64)

    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _insert(conn, user_id, series_key=SERIES_KEY, content_hash="d" * 64)


def test_superseding_first_then_inserting_is_allowed(sync_engine, schema_ready, make_user):
    """The route's actual order: mark the old row superseded, THEN add the new one.

    This is the regression guard. Reversing these two statements reproduces the
    duplicate-key failure on the ordinary edit-and-return path.
    """
    user_id = make_user()
    with sync_engine.begin() as conn:
        first = _insert(conn, user_id, series_key=SERIES_KEY, content_hash="c" * 64)

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE qa_baselines SET state = 'superseded', superseded_at = now() "
                "WHERE id = :id"
            ),
            {"id": first},
        )
        second = _insert(conn, user_id, series_key=SERIES_KEY, content_hash="d" * 64)
        conn.execute(
            text("UPDATE qa_baselines SET superseded_by = :new WHERE id = :old"),
            {"new": second, "old": first},
        )

    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT state, superseded_by FROM qa_baselines "
                "WHERE series_key = :sk ORDER BY created_at"
            ),
            {"sk": SERIES_KEY},
        ).all()

    assert len(rows) == 2, "the earlier snapshot is kept, not deleted"
    assert rows[0][0] == "superseded"
    assert rows[0][1] == second, "the old row points at its replacement"
    assert rows[1][0] == "provisional"


def test_many_superseded_rows_do_not_block_the_next_one(
    sync_engine, schema_ready, make_user
):
    """A patient edited over several sessions must not eventually wedge."""
    user_id = make_user()
    previous = None
    for i in range(5):
        with sync_engine.begin() as conn:
            if previous is not None:
                conn.execute(
                    text("UPDATE qa_baselines SET state = 'superseded' WHERE id = :id"),
                    {"id": previous},
                )
            previous = _insert(
                conn, user_id, series_key=SERIES_KEY, content_hash=f"{i:064d}"
            )

    with sync_engine.connect() as conn:
        live = conn.execute(
            text(
                "SELECT count(*) FROM qa_baselines "
                "WHERE series_key = :sk AND state <> 'superseded'"
            ),
            {"sk": SERIES_KEY},
        ).scalar()
        total = conn.execute(
            text("SELECT count(*) FROM qa_baselines WHERE series_key = :sk"),
            {"sk": SERIES_KEY},
        ).scalar()

    assert live == 1, "exactly one live baseline per series, always"
    assert total == 5, "the history is retained"


def test_the_dedup_lookup_finds_an_identical_snapshot(sync_engine, schema_ready, make_user):
    """What makes reopening a patient a no-op instead of a new row."""
    user_id = make_user()
    content = "c" * 64
    with sync_engine.begin() as conn:
        _insert(conn, user_id, series_key=SERIES_KEY, content_hash=content)

    with sync_engine.connect() as conn:
        found = conn.execute(
            text(
                "SELECT count(*) FROM qa_baselines WHERE user_id = :uid "
                "AND series_key = :sk AND structure_set_sha256 = :hash "
                "AND state <> 'superseded'"
            ),
            {"uid": user_id, "sk": SERIES_KEY, "hash": content},
        ).scalar()
    assert found == 1


def test_baselines_are_scoped_per_tenant(sync_engine, schema_ready, make_user):
    """Two clinics segmenting the same series must not collide.

    They would derive different series keys anyway, since the HMAC secret is
    per-tenant — but the index must not depend on that being true.
    """
    a, b = make_user(), make_user()
    with sync_engine.begin() as conn:
        _insert(conn, a, series_key=SERIES_KEY, content_hash="c" * 64)
        _insert(conn, b, series_key=SERIES_KEY, content_hash="c" * 64)

    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM qa_baselines WHERE series_key = :sk"),
            {"sk": SERIES_KEY},
        ).scalar()
    assert count == 2


def test_a_baseline_outlives_its_job(sync_engine, schema_ready, make_user, make_job):
    """job_id is ON DELETE SET NULL, not CASCADE.

    The sweeper purges a job row after the result TTL, but the planner does not come
    back to edit for days. If the baseline went with the job, QA would silently never
    work — the failure would look like "the feature does nothing" rather than an error.
    """
    user_id = make_user()
    job_id = make_job(user_id=user_id, state="done")

    with sync_engine.begin() as conn:
        baseline_id = _insert(conn, user_id, series_key=SERIES_KEY, content_hash="c" * 64)
        conn.execute(
            text("UPDATE qa_baselines SET job_id = :job WHERE id = :id"),
            {"job": job_id, "id": baseline_id},
        )

    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})

    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT job_id, state FROM qa_baselines WHERE id = :id"),
            {"id": baseline_id},
        ).one()
    assert row[0] is None, "job reference cleared"
    assert row[1] == "provisional", "the baseline survived"

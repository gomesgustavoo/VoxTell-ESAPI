"""Lease reclaim: recovering jobs whose worker died, stalled, or ran too long.

The behaviour these pin is the answer to the worst failure mode in the system. A
job wedged inside the GPU call used to heartbeat forever, keep the pod's liveness
probe green forever, and hold the Postgres advisory GPU lock forever — which
silently blocked DicomSegVR's inference pod in another namespace, in another
product, with no alerting. The lease is what makes that recoverable.

Two clocks with deliberately opposite expiry semantics:

* ``lease_expires_at`` expiring means "the worker is dead or stalled" — recoverable,
  so requeue up to ``WORKER_MAX_ATTEMPTS``.
* ``deadline_at`` expiring means "this job is pathological" — **not** recoverable, so
  fail terminally even with attempts left. Retrying a job that wedges on one input
  just spends another hour of a shared GPU.

``test_a_running_job_without_a_lease_is_untouched`` is the rollout guard. The API
ships before the worker, so for a while the *old* worker is claiming jobs and never
setting ``lease_expires_at``. If reclaim treated NULL as expired it would requeue
every job that worker is actively running, mid-flight, on a 30-second cycle.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest

from api import reclaim
from api.config import settings

pytestmark = pytest.mark.pg

# A lease/deadline far enough in the past to be unambiguous, expressed in SQL
# because Postgres evaluates now(), not Python. See tests/conftest.py.
_PAST = "now() - interval '5 minutes'"
_FUTURE = "now() + interval '10 minutes'"


async def test_expired_lease_with_attempts_left_is_requeued(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    jid = make_job(
        make_user(), state="running", attempts=1, worker_id="dead-worker", progress=0.4,
    )
    # Backdate the lease in SQL rather than inserting a NULL one: ck_jobs_running_has_lease
    # forbids a lease-less running row, and a bound Python datetime would be compared
    # against the database's clock, which is not guaranteed to agree with ours.
    with worker_db.begin() as conn:
        from sqlalchemy import text
        conn.execute(
            text(f"UPDATE jobs SET lease_expires_at = {_PAST} WHERE id = :j"), {"j": jid}
        )

    actions = await reclaim.reclaim_once()

    assert actions["lease_expired_requeued"] == 1
    row = job_state(jid, "state", "worker_id", "progress", "lease_expires_at", "attempts")
    assert row["state"] == "queued"
    assert row["worker_id"] is None, "a requeued job must not claim a dead owner"
    assert row["progress"] == 0
    assert row["lease_expires_at"] is None
    assert row["attempts"] == 1, "attempts is incremented by the claim, not by reclaim"


async def test_requeued_job_keeps_its_original_queued_at(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    """Resetting it would send an already-waiting job to the back of the queue.

    Preserving it is also what lets the claim's aging clause promote the job, so a
    worker crash does not cost the user their place.
    """
    from sqlalchemy import text

    jid = make_job(
        make_user(), state="running", attempts=1,
        created_at="now() - interval '30 minutes'",
        queued_at="now() - interval '25 minutes'",
    )
    before = job_state(jid, "queued_at")["queued_at"]
    with worker_db.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET lease_expires_at = {_PAST} WHERE id = :j"), {"j": jid}
        )

    await reclaim.reclaim_once()

    assert job_state(jid, "queued_at")["queued_at"] == before


async def test_expired_lease_with_no_attempts_left_fails_as_stalled(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    from sqlalchemy import text

    jid = make_job(make_user(), state="running", attempts=settings.WORKER_MAX_ATTEMPTS)
    with worker_db.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET lease_expires_at = {_PAST} WHERE id = :j"), {"j": jid}
        )

    actions = await reclaim.reclaim_once()

    assert actions["attempts_exhausted_failed"] == 1
    row = job_state(jid, "state", "failure_class", "error", "finished_at")
    assert row["state"] == "failed"
    assert row["failure_class"] == "stalled"
    assert row["finished_at"] is not None
    assert "repeatedly" in row["error"]


async def test_expired_deadline_fails_terminally_even_with_attempts_left(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    """The deliberate asymmetry: a pathological job is not retried.

    Attempts are deliberately at zero here — the point is that having budget left
    does not save it. A job that wedges on one input wedges again, and each retry
    costs another hour of a GPU shared with another product.
    """
    from sqlalchemy import text

    jid = make_job(make_user(), state="running", attempts=0)
    with worker_db.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET deadline_at = {_PAST}, lease_expires_at = {_FUTURE} "
                 "WHERE id = :j"),
            {"j": jid},
        )

    actions = await reclaim.reclaim_once()

    assert actions["deadline_failed"] == 1
    assert actions["lease_expired_requeued"] == 0, "a timed-out job must not be requeued"
    row = job_state(jid, "state", "failure_class")
    assert row["state"] == "failed"
    assert row["failure_class"] == "timeout"


async def test_a_live_lease_is_untouched(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    from sqlalchemy import text

    jid = make_job(make_user(), state="running", worker_id="healthy")
    with worker_db.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET lease_expires_at = {_FUTURE}, deadline_at = {_FUTURE} "
                 "WHERE id = :j"),
            {"j": jid},
        )

    actions = await reclaim.reclaim_once()

    assert sum(actions.values()) == 0
    assert job_state(jid, "state", "worker_id") == {"state": "running", "worker_id": "healthy"}


async def test_a_running_job_without_a_lease_is_untouched(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    """The rollout guard. NULL means "claimed by the old worker", not "expired".

    The API ships ahead of the worker, so during that window the previous worker image
    is claiming jobs and setting no lease at all. Treating NULL as expired would requeue
    every job it is actively running, every 30 seconds, forever.

    ``ck_jobs_running_has_lease`` now makes that row impossible to create — but the
    constraint is added *after* the new worker is live, so the state genuinely existed in
    production during the rollout, and the reclaim query must be defensive on its own
    rather than relying on a constraint that did not yet exist. This test drops the
    constraint to reconstruct that state and puts it back afterwards.
    """
    from sqlalchemy import text

    jid = make_job(make_user(), state="running", worker_id="old-image-worker")
    with worker_db.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP CONSTRAINT ck_jobs_running_has_lease"))
    try:
        with worker_db.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET lease_expires_at = NULL, deadline_at = NULL "
                     "WHERE id = :j"),
                {"j": jid},
            )

        actions = await reclaim.reclaim_once()

        assert sum(actions.values()) == 0, "reclaim must ignore pre-lease jobs entirely"
        assert job_state(jid, "state")["state"] == "running"
    finally:
        with worker_db.begin() as conn:
            conn.execute(
                text("ALTER TABLE jobs ADD CONSTRAINT ck_jobs_running_has_lease CHECK ("
                     "state <> 'running' OR lease_expires_at IS NOT NULL) NOT VALID")
            )


async def test_reclaim_is_idempotent(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
) -> None:
    """A second pass finds nothing — the first pass moved the row out of scope."""
    from sqlalchemy import text

    jid = make_job(make_user(), state="running", attempts=1)
    with worker_db.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET lease_expires_at = {_PAST} WHERE id = :j"), {"j": jid}
        )

    first = await reclaim.reclaim_once()
    second = await reclaim.reclaim_once()

    assert first["lease_expired_requeued"] == 1
    assert sum(second.values()) == 0


async def test_non_running_states_are_never_reclaimed(
    worker_db, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
) -> None:
    """A stale lease on a finished row must not resurrect it."""
    from sqlalchemy import text

    for state in ("queued", "done", "failed", "cancelled", "expired", "awaiting_upload"):
        jid = make_job(make_user(), state=state, queued_at=None)
        with worker_db.begin() as conn:
            conn.execute(
                text(f"UPDATE jobs SET lease_expires_at = {_PAST}, deadline_at = {_PAST} "
                     "WHERE id = :j"),
                {"j": jid},
            )

    assert sum((await reclaim.reclaim_once()).values()) == 0


def test_reclaim_lock_key_is_distinct() -> None:
    """Advisory locks share one namespace per database."""
    from api import db as api_db
    from api import sweeper
    from worker.settings import settings as worker_settings

    keys = [
        reclaim._RECLAIM_LOCK_KEY,
        api_db._SCHEMA_LOCK_KEY,
        sweeper._SWEEP_LOCK_KEY,
        worker_settings.GPU_LOCK_KEY,
    ]
    assert len(set(keys)) == len(keys), f"advisory key collision: {keys}"

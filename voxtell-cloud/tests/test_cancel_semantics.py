"""Cancellation must actually cancel — including the race that used to lose.

THE BUG
-------
Upstream only raises ``InferenceCancelled`` if the cancel arrives *while* it is still
iterating patches. A cancel landing in the window between ``engine.segment`` returning
normally and the job being recorded was never re-read, so the job was written **done**,
a result was uploaded, and a ``UsageEvent`` was charged — for work the user had
explicitly asked to stop. The planner saw "Cancelling…" followed by a completed job and
a spent quota unit.

``worker/main.py`` now checks at three boundaries (before the GPU lock, after
``segment`` returns, after postprocess). Those are three call sites a refactor could
quietly drop, so ``finish_success`` also refuses to overwrite a cancellation. These
tests pin the guarantee at that lower level, where it cannot be bypassed.

Why not a CHECK constraint instead: it would turn the race into a failed transaction,
leaving the row ``running`` with its result orphaned until reclaim. A guarded UPDATE
lets the worker learn the truth and record the cancellation.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from worker import job as worker_job

pytestmark = pytest.mark.pg


def _request_cancel(engine: Engine, job_id: uuid.UUID) -> None:
    """What POST /v1/jobs/{id}/cancel does to a running job: set the flag only."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET cancel_requested = true WHERE id = :j"), {"j": job_id}
        )


def test_finish_success_records_done_normally(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    jid = make_job(make_user(), state="running")

    assert worker_job.finish_success(
        jid, result_key="u/x/result.json.gz", mask_key=None, gpu_seconds=2.5, message="ok"
    ) is True

    row = job_state(jid, "state", "result_key", "progress", "lease_expires_at")
    assert row["state"] == "done"
    assert row["result_key"] == "u/x/result.json.gz"
    assert row["progress"] == 1
    assert row["lease_expires_at"] is None


def test_a_cancelled_job_is_never_recorded_done(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    """THE regression test. finish_success must refuse a cancelled job."""
    jid = make_job(make_user(), state="running")
    _request_cancel(worker_db, jid)

    assert worker_job.finish_success(
        jid, result_key="u/x/result.json.gz", mask_key=None, gpu_seconds=2.5, message="ok"
    ) is False

    row = job_state(jid, "state", "result_key", "cancel_requested", "finished_at")
    assert row["state"] == "cancelled", "a cancelled job was recorded as done"
    assert row["result_key"] is None, (
        "no result may be offered for a job the user cancelled"
    )
    assert row["cancel_requested"] is True
    assert row["finished_at"] is not None


def test_a_cancelled_job_is_not_charged_gpu_time(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
) -> None:
    """The usage row must not be backfilled with GPU seconds for cancelled work."""
    uid = make_user()
    jid = make_job(uid, state="running")
    with worker_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO usage_events (id, user_id, job_id, prompts) "
                "VALUES (:id, :uid, :jid, 1)"
            ),
            {"id": uuid.uuid4(), "uid": uid, "jid": jid},
        )
    _request_cancel(worker_db, jid)

    worker_job.finish_success(
        jid, result_key="k", mask_key=None, gpu_seconds=42.0, message="ok"
    )

    with worker_db.connect() as conn:
        gpu = conn.execute(
            text("SELECT gpu_seconds FROM usage_events WHERE job_id = :j"), {"j": jid}
        ).scalar()
    assert gpu is None, "cancelled work was billed GPU seconds"


def test_finish_cancelled_clears_both_clocks(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    jid = make_job(make_user(), state="running")
    with worker_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET lease_expires_at = now() + interval '5 minutes', "
                "deadline_at = now() + interval '1 hour' WHERE id = :j"
            ),
            {"j": jid},
        )

    worker_job.finish_cancelled(jid)

    row = job_state(jid, "state", "lease_expires_at", "deadline_at")
    assert row["state"] == "cancelled"
    assert row["lease_expires_at"] is None and row["deadline_at"] is None


def test_cancel_requested_is_readable_by_the_worker(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
) -> None:
    jid = make_job(make_user(), state="running")
    assert worker_job.cancel_requested(jid) is False
    _request_cancel(worker_db, jid)
    assert worker_job.cancel_requested(jid) is True


def test_cancel_watcher_latches(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
) -> None:
    """Once true it stays true without further queries — it is polled per patch."""
    jid = make_job(make_user(), state="running")
    watcher = worker_job.CancelWatcher(jid, interval=0.0)

    assert watcher() is False
    _request_cancel(worker_db, jid)
    assert watcher() is True

    # Clearing the flag in the database must not un-cancel an unwinding job.
    with worker_db.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET cancel_requested = false WHERE id = :j"), {"j": jid}
        )
    assert watcher() is True, "the watcher must latch, not re-poll after cancelling"


def test_transient_failure_requeues_with_backoff(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    jid = make_job(make_user(), state="running", attempts=1)

    assert worker_job.finish_transient_failure(jid, "SeaweedFS 503", 45.0) is True

    row = job_state(jid, "state", "not_before", "failure_class", "worker_id", "queued_at")
    assert row["state"] == "queued"
    assert row["not_before"] is not None, "a retry must be deferred, not immediate"
    assert row["failure_class"] == "transient"
    assert row["worker_id"] is None
    assert row["queued_at"] is not None, (
        "queued_at must survive a retry so the aging clause can promote the job"
    )


def test_transient_failure_gives_up_when_out_of_attempts(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    """The attempts check is in SQL against the row, so two workers cannot both retry."""
    from worker.settings import settings

    jid = make_job(make_user(), state="running", attempts=settings.WORKER_MAX_ATTEMPTS)

    assert worker_job.finish_transient_failure(jid, "SeaweedFS 503", 45.0) is False

    row = job_state(jid, "state", "failure_class", "error")
    assert row["state"] == "failed"
    assert row["failure_class"] == "transient", (
        "the failure was transient in KIND even though it is now terminal — the label "
        "is what tells you infrastructure is flaky rather than the input being bad"
    )
    assert "Repeated temporary failures" in row["error"]

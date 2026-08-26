"""ProgressReporter: the write throttle must never eat a change of message.

Regression cover for a bug found by the live GPU-contention test on 2026-08-12.
``worker/main.py::process`` writes ``(0.20, "Preparing inference")`` and then
``engine.segment`` immediately calls ``(0.16, "Waiting for the GPU")`` when the
cross-service GPU mutex is held by DicomSegVR. Both throttle conditions were
true — the second call lands inside the 2 s window, and the fraction *decreases*
so the delta is negative — so the write was dropped, and a planner queued behind
the other product watched "Preparing inference" sit motionless for the entire
wait. That is precisely the frozen-looking job the message exists to prevent.

These are ``pg`` tests on purpose: the bug lived in the interaction between the
throttle decision and the UPDATE, so asserting on the row is the only assertion
that would have caught it. Time comes from ``time.monotonic`` inside the
reporter, and the whole point is that these calls happen within the 2 s window,
so no clock is patched — the test simply calls twice in a row.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.pg


def test_message_change_is_written_even_inside_the_throttle_window(
    worker_db, make_user, make_job, job_state
) -> None:
    """The exact live sequence: 0.20 then a LOWER 0.16 with a new message."""
    from worker import job as worker_job

    user_id = make_user()
    # make_job supplies the lease and deadline itself for a running job, because
    # ck_jobs_running_has_lease forbids a running row without one.
    jid = make_job(user_id, state="running")

    reporter = worker_job.ProgressReporter(jid)
    reporter(0.20, "Preparing inference")
    reporter(0.16, "Waiting for the GPU")

    row = job_state(jid, "progress", "message")
    assert row["message"] == "Waiting for the GPU"
    assert row["progress"] == pytest.approx(0.16)


def test_same_message_inside_the_window_is_still_throttled(
    worker_db, make_user, make_job, job_state
) -> None:
    """The write storm the throttle exists for must still be suppressed.

    The per-patch callback repeats one message with a slowly rising fraction —
    hundreds of calls a minute. Only the message-change bypass was widened, so a
    sub-delta repeat of the SAME message must not reach Postgres.
    """
    from worker import job as worker_job

    user_id = make_user()
    # make_job supplies the lease and deadline itself for a running job, because
    # ck_jobs_running_has_lease forbids a running row without one.
    jid = make_job(user_id, state="running")

    reporter = worker_job.ProgressReporter(jid)
    reporter(0.50, "Segmenting")
    reporter(0.505, "Segmenting")  # +0.005: under min_delta, inside min_interval

    row = job_state(jid, "progress")
    assert row["progress"] == pytest.approx(0.50), "throttle must still suppress a same-message repeat"


def test_large_delta_is_written_even_for_the_same_message(
    worker_db, make_user, make_job, job_state
) -> None:
    """A jump past min_delta is real progress and must land regardless of timing."""
    from worker import job as worker_job

    user_id = make_user()
    # make_job supplies the lease and deadline itself for a running job, because
    # ck_jobs_running_has_lease forbids a running row without one.
    jid = make_job(user_id, state="running")

    reporter = worker_job.ProgressReporter(jid)
    reporter(0.30, "Segmenting")
    reporter(0.90, "Segmenting")

    assert job_state(jid, "progress")["progress"] == pytest.approx(0.90)


def test_terminal_states_are_never_overwritten(
    worker_db, make_user, make_job, job_state
) -> None:
    """The UPDATE is guarded on ``state = 'running'``.

    A late callback arriving after the job was cancelled must not resurrect a
    progress value onto a finished row, and the message-change bypass must not
    have widened that hole.
    """
    from worker import job as worker_job

    user_id = make_user()
    jid = make_job(user_id, state="cancelled", queued_at="now()")

    reporter = worker_job.ProgressReporter(jid)
    reporter(0.44, "Waiting for the GPU")

    row = job_state(jid, "progress", "message", "state")
    assert row["state"] == "cancelled"
    assert row["progress"] == pytest.approx(0.0)
    assert row["message"] != "Waiting for the GPU"


def test_lease_is_touched_before_the_throttle(make_user) -> None:
    """Every call is progress evidence, even the ones that do not write.

    Touching only on writes that get through would make a job progressing
    steadily but slowly look stalled to the watchdog — and being marked stalled
    now withholds ``health.touch()``, which kills the pod.
    """
    from worker import job as worker_job

    class FakeLease:
        def __init__(self) -> None:
            self.touches = 0

        def touch(self) -> None:
            self.touches += 1

    lease = FakeLease()
    # No DB fixture: the write will fail, and the reporter swallows that by design.
    reporter = worker_job.ProgressReporter(uuid.uuid4(), lease=lease)
    for _ in range(5):
        try:
            reporter(0.5, "Segmenting")
        except Exception:  # pragma: no cover - the DB is deliberately absent here
            pass

    assert lease.touches == 5, "the lease must be touched on every call, throttled or not"

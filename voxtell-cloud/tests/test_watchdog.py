"""Stall detection — the mechanism that ends the cross-product GPU deadlock.

Pure unit tests over ``worker/watchdog.py``. No database, no GPU: the whole point of
that module is that "is this job progressing?" is answerable in-process, which is what
makes it answerable *while the GPU thread is wedged*.

What went wrong before, and what these tests protect:

The old heartbeat loop wrote ``heartbeat_at`` and touched ``/tmp/alive`` on a plain
30-second timer. A timer keeps ticking while a CUDA call never returns, so a wedged job
(a) kept heartbeating and was never swept, (b) kept the exec liveness probe green so the
pod was never recycled, and (c) went on holding the Postgres advisory GPU lock —
blocking DicomSegVR's inference pod, in another namespace, for another product's users,
with nothing connecting the two.

``test_any_stalled_is_true_if_a_single_job_is_stalled`` is the load-bearing one. The
liveness file is a property of the *process*, and one wedged job is reason enough to
recycle it, because that job is the one holding the mutex. Killing a pod that also had a
healthy job in flight costs that job a requeue, which the reclaim loop handles; leaving
a second product blocked has no equivalent recovery.
"""

from __future__ import annotations

import uuid

import pytest

from worker import watchdog
from worker.settings import settings


@pytest.fixture
def tight_grace(monkeypatch: pytest.MonkeyPatch):
    """Shrink the grace budgets so a test can express a stall without sleeping.

    Patched on the settings object rather than the module, because watchdog reads them
    through lambdas at call time — which is itself deliberate, so a ConfigMap change
    does not need a code change.
    """
    monkeypatch.setattr(settings, "WORKER_STALL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(settings, "WORKER_STALL_GRACE_IO_SECONDS", 0.10)
    monkeypatch.setattr(settings, "WORKER_GPU_WAIT_MAX_SECONDS", 5.00)


def _stale(lease: watchdog.JobLease, seconds: float) -> None:
    """Backdate the lease's last-progress mark. Monotonic clock, so subtract directly."""
    lease._last -= seconds  # noqa: SLF001 - the point is to simulate elapsed time


def test_a_fresh_lease_is_not_stalled() -> None:
    lease = watchdog.JobLease(uuid.uuid4())
    assert not lease.is_stalled()
    assert lease.stalled_for() < 1.0


def test_silence_past_the_grace_period_is_a_stall(tight_grace) -> None:
    lease = watchdog.JobLease(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    _stale(lease, 10.0)
    assert lease.is_stalled()


def test_progress_clears_a_stall(tight_grace) -> None:
    """A single touch is enough — the point is evidence of life, not a rate."""
    lease = watchdog.JobLease(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    _stale(lease, 10.0)
    assert lease.is_stalled()

    lease.touch()
    assert not lease.is_stalled()


def test_waiting_for_the_gpu_gets_a_much_larger_budget(tight_grace) -> None:
    """Blocking on pg_advisory_lock is LEGITIMATE non-progress.

    Without its own budget, a long queue behind DicomSegVR would be indistinguishable
    from a wedged job and we would recycle our own pod for waiting its turn politely.
    """
    lease = watchdog.JobLease(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    _stale(lease, 1.0)
    assert lease.is_stalled(), "1 s of compute silence should trip the tightened budget"

    lease.phase(watchdog.PHASE_WAITING_FOR_GPU)
    _stale(lease, 1.0)
    assert not lease.is_stalled(), "waiting for the GPU must not count as a stall"
    assert lease.grace() > settings.WORKER_STALL_GRACE_SECONDS


def test_io_phases_tolerate_longer_silence(tight_grace) -> None:
    """One S3 download or one write_seg per prompt reports nothing while it runs."""
    lease = watchdog.JobLease(uuid.uuid4(), watchdog.PHASE_IO)
    assert lease.grace() > watchdog.JobLease(uuid.uuid4(), watchdog.PHASE_COMPUTE).grace()


def test_a_phase_change_counts_as_progress(tight_grace) -> None:
    lease = watchdog.JobLease(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    _stale(lease, 10.0)
    lease.phase(watchdog.PHASE_IO)
    assert not lease.is_stalled()


def test_a_stall_is_reported_once_per_episode(tight_grace) -> None:
    """So the 30-second loop logs and counts once, not on every tick."""
    lease = watchdog.JobLease(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    _stale(lease, 10.0)

    assert lease.note_stall_once() == watchdog.PHASE_COMPUTE
    assert lease.note_stall_once() is None, "a single stall must not be counted twice"

    # Recovery then a fresh stall is a NEW episode and must be reported again.
    lease.touch()
    _stale(lease, 10.0)
    assert lease.note_stall_once() == watchdog.PHASE_COMPUTE


def test_note_stall_once_is_silent_for_a_healthy_job(tight_grace) -> None:
    assert watchdog.JobLease(uuid.uuid4()).note_stall_once() is None


# ------------------------------------------------------------------- the registry


def test_registry_tracks_and_forgets(tight_grace) -> None:
    reg = watchdog.LeaseRegistry()
    jid = uuid.uuid4()
    reg.add(jid)
    assert [j for j, _ in reg.items()] == [jid]
    reg.remove(jid)
    assert reg.items() == []


def test_any_stalled_is_false_when_all_jobs_progress(tight_grace) -> None:
    reg = watchdog.LeaseRegistry()
    for _ in range(3):
        reg.add(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    assert not reg.any_stalled()


def test_any_stalled_is_true_if_a_single_job_is_stalled(tight_grace) -> None:
    """THE deadlock fix, in one assertion.

    Deliberately any-not-all: the stalled job is the one holding the cross-product GPU
    mutex, so the process must be recycled even though its neighbours are fine. The
    healthy job's cost is a requeue, which the reclaim loop handles; a second product
    blocked indefinitely has no such recovery.

    ``any_stalled()`` being True is what makes ``lease_loop`` skip ``health.touch()``,
    so /tmp/alive goes stale, the existing exec probe kills the pod, the lock
    connection dies, and Postgres releases the session-level advisory lock for free.
    """
    reg = watchdog.LeaseRegistry()
    healthy = reg.add(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    wedged = reg.add(uuid.uuid4(), watchdog.PHASE_COMPUTE)
    _stale(wedged, 10.0)

    assert reg.any_stalled(), (
        "one wedged job must be enough to stop asserting process liveness — it is the "
        "job holding the GPU mutex that DicomSegVR is blocked behind"
    )
    assert not healthy.is_stalled(), "the neighbour was healthy; only the process is condemned"


def test_registry_is_safe_from_concurrent_threads(tight_grace) -> None:
    """touch() is called from the GPU thread, the process pool path and the loop."""
    import threading

    reg = watchdog.LeaseRegistry()
    leases = [reg.add(uuid.uuid4()) for _ in range(8)]
    errors: list[BaseException] = []

    def hammer(lease):
        try:
            for _ in range(500):
                lease.touch()
                lease.is_stalled()
                reg.any_stalled()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(x,)) for x in leases]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, f"thread-safety violation: {errors[:2]}"


def test_shipped_grace_defaults_are_ordered_sensibly() -> None:
    """compute < io < waiting_for_gpu, and none of them accidentally zero.

    A zero or inverted budget would either recycle the pod constantly or never — both
    of which reintroduce the bug from opposite directions.
    """
    assert 0 < settings.WORKER_STALL_GRACE_SECONDS < settings.WORKER_STALL_GRACE_IO_SECONDS
    assert settings.WORKER_STALL_GRACE_IO_SECONDS < settings.WORKER_GPU_WAIT_MAX_SECONDS
    # The hard mutex ceiling must exceed the longest legitimate wait, or a job that
    # waited its turn would have its own lock torn out from under it on acquisition.
    assert settings.WORKER_GPU_LOCK_MAX_HOLD_SECONDS > settings.WORKER_GPU_WAIT_MAX_SECONDS

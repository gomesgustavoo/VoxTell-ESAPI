"""Stall detection: the difference between a slow job and a wedged one.

THE BUG THIS EXISTS TO FIX
--------------------------
The old ``heartbeat_loop`` wrote ``heartbeat_at`` for every in-flight job **and**
touched the liveness file, unconditionally, every 30 seconds. Both were driven by a
plain asyncio timer, which keeps ticking happily while the GPU thread is wedged
inside a CUDA call. The consequences compounded:

1. the job kept heartbeating, so nothing ever swept it;
2. ``/tmp/alive`` kept being touched, so the exec liveness probe never fired and the
   pod was never restarted;
3. and the wedged job still held the Postgres advisory GPU lock — which is
   **cross-product**. DicomSegVR's inference pod, in another namespace, serving other
   users, blocked indefinitely. No alert connected the two.

So the fix is not "add a timeout somewhere". It is to stop asserting liveness on a
timer and start asserting it on *observed progress*. Then the existing machinery does
the rest for free: the liveness file goes stale, the kubelet kills the pod, the
Postgres connection dies, the advisory lock is released automatically (session-scoped,
no reaper to get wrong), DicomSegVR resumes, and the job's lease expires so the API's
reclaim loop requeues it.

WHY PER-PHASE GRACE PERIODS
---------------------------
"No progress for 5 minutes" means different things in different phases, and a single
threshold has to be set for the most tolerant one — which makes it useless for the
rest. Three budgets:

* ``compute`` — the GPU is reporting per-patch progress, so silence is suspicious fast.
* ``io`` — a single S3 download or one ``write_seg`` per prompt reports nothing while
  it runs; minutes of silence here is normal.
* ``waiting_for_gpu`` — blocking on ``pg_advisory_lock`` is *legitimate* non-progress.
  Without its own budget, a long queue behind DicomSegVR would be indistinguishable
  from a wedged job, and we would kill our own pod for waiting its turn politely.

Everything here is deliberately dependency-free and thread-safe: ``touch()`` is called
from the GPU thread, the process-pool callback path and the event loop.
"""

from __future__ import annotations

import logging
import threading
import time

from .settings import settings

log = logging.getLogger("worker.watchdog")

# Phase names. Kept as a small closed vocabulary because they are also metric label
# values, so they must not grow per-job.
PHASE_COMPUTE = "compute"
PHASE_IO = "io"
PHASE_WAITING_FOR_GPU = "waiting_for_gpu"

_GRACE = {
    PHASE_COMPUTE: lambda: settings.WORKER_STALL_GRACE_SECONDS,
    PHASE_IO: lambda: settings.WORKER_STALL_GRACE_IO_SECONDS,
    PHASE_WAITING_FOR_GPU: lambda: settings.WORKER_GPU_WAIT_MAX_SECONDS,
}


class JobLease:
    """Tracks whether one job is still visibly making progress.

    Call :meth:`touch` from anything that constitutes evidence of progress — a
    per-patch callback, a stage boundary, an upstream notice. Call :meth:`phase` when
    entering a differently-paced part of the pipeline.
    """

    def __init__(self, job_id, phase: str = PHASE_IO) -> None:
        self._job_id = job_id
        self._lock = threading.Lock()
        self._phase = phase
        self._last = time.monotonic()
        self._stalled_reported = False

    # -- progress ---------------------------------------------------------------
    def touch(self, phase: str | None = None) -> None:
        with self._lock:
            if phase is not None:
                self._phase = phase
            self._last = time.monotonic()
            self._stalled_reported = False

    def phase(self, phase: str) -> None:
        """Enter a new phase. Resets the clock — a phase change IS progress."""
        self.touch(phase)

    # -- interrogation ----------------------------------------------------------
    @property
    def current_phase(self) -> str:
        with self._lock:
            return self._phase

    def stalled_for(self) -> float:
        with self._lock:
            return time.monotonic() - self._last

    def grace(self) -> float:
        with self._lock:
            phase = self._phase
        return _GRACE.get(phase, _GRACE[PHASE_COMPUTE])()

    def is_stalled(self) -> bool:
        """True when this job has been silent longer than its phase allows."""
        return self.stalled_for() > self.grace()

    def note_stall_once(self) -> str | None:
        """Return the phase the first time a stall is observed, else None.

        Lets the caller log and count a stall exactly once per stall episode instead
        of on every 30-second tick.
        """
        if not self.is_stalled():
            return None
        with self._lock:
            if self._stalled_reported:
                return None
            self._stalled_reported = True
            phase = self._phase
            silent = time.monotonic() - self._last
        log.error(
            "job %s has made no progress for %.0fs in phase %r — withholding its lease "
            "renewal and the liveness touch so this pod is recycled and the GPU mutex "
            "released",
            self._job_id, silent, phase,
        )
        return phase


class LeaseRegistry:
    """The in-flight leases, so the renewal loop can ask about all of them at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[object, JobLease] = {}

    def add(self, job_id, phase: str = PHASE_IO) -> JobLease:
        lease = JobLease(job_id, phase)
        with self._lock:
            self._leases[job_id] = lease
        return lease

    def remove(self, job_id) -> None:
        with self._lock:
            self._leases.pop(job_id, None)

    def items(self) -> list[tuple[object, JobLease]]:
        with self._lock:
            return list(self._leases.items())

    def any_stalled(self) -> bool:
        """True if ANY in-flight job is stalled.

        Deliberately any-not-all: the liveness file is a property of the *process*, and
        one wedged job is enough reason to recycle it — that job is the one holding the
        GPU mutex. Killing a pod that also had a healthy job in flight costs that job a
        requeue, which the reclaim loop handles; leaving a second product blocked does
        not have an equivalent recovery.
        """
        return any(lease.is_stalled() for _, lease in self.items())

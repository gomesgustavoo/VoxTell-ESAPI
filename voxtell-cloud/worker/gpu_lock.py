"""Cross-service GPU mutex.

This box has one RTX 3080 and two GPU workers want it: VoxTell-Cloud and
DicomSegVR's nnU-Net/TotalSegmentator inference pod. The NVIDIA device plugin is
configured for time-slicing (``replicas: 2``) so both pods can *schedule*, but
time-slicing partitions compute, not VRAM — two large 3D models running at once
would OOM. This lock makes sure only one of them is ever mid-inference.

A Postgres **session-level advisory lock** is exactly the right primitive:
  * it is held by a connection, so a pod crash releases it automatically —
    no stale-lock reaper needed;
  * ``pg_advisory_lock`` blocks and queues fairly, which is what we want (wait
    your turn) rather than failing fast;
  * both services already talk to the same Postgres.

Advisory locks are scoped to a *database*, and the two services use different
ones, so the lock is taken on a third, empty database (``gpulock``) that both
can connect to with nothing but CONNECT rights.

Set ``GPU_LOCK_DSN`` to empty to disable — the local docker-compose stack and
any single-tenant GPU need no mutex.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Iterator

from sqlalchemy import create_engine, text

from . import metrics
from .settings import settings

log = logging.getLogger("worker.gpulock")

# One dedicated engine, one connection at a time: a session-level advisory lock
# belongs to the connection that took it, so it must be the same connection that
# releases it. pool_size=1 guarantees that without threading the connection
# object through the call chain.
_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = create_engine(
                    settings.GPU_LOCK_DSN,
                    pool_pre_ping=True,
                    pool_size=1,
                    max_overflow=0,
                    echo=False,
                )
    return _engine


class Transient(RuntimeError):
    """The lock database is unreachable — retry rather than fail the job.

    Named so ``worker/failures.py`` classifies it by name without importing this
    module. Before this existed, an unreachable ``gpulock`` database made
    ``gpu_lock()`` raise something generic, which was caught as a normal exception and
    **permanently failed every job**. Losing the mutex should degrade throughput, not
    destroy work.
    """


def _force_release(pid: int) -> None:
    """Kill our own lock-holding backend from a fresh connection.

    The last-resort release for the cross-product deadlock. If this worker has held
    the mutex past the hard ceiling, DicomSegVR's inference pod has been blocked that
    whole time, in another namespace, for another product's users. Terminating the
    backend drops the session and Postgres releases the session-level advisory lock
    immediately — without waiting for the kubelet to notice the stale liveness file,
    which is minutes slower.

    Runs on a *separate* connection because the wedged one is, by definition, not
    going to execute anything. Same ``gpulock`` role, so no extra grant is needed.
    """
    try:
        with _get_engine().connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            conn.commit()
        log.error(
            "force-released the GPU mutex after %.0fs by terminating backend %d — a job "
            "is wedged and DicomSegVR was blocked behind it",
            settings.WORKER_GPU_LOCK_MAX_HOLD_SECONDS, pid,
        )
    except Exception as exc:
        log.error("could not force-release the GPU mutex (backend %d): %s", pid, exc)


@contextlib.contextmanager
def gpu_lock(on_wait=None) -> Iterator[None]:
    """Hold the GPU for the duration of the block.

    ``on_wait(seconds)`` is invoked once if the lock is not immediately free, so
    the caller can surface "Waiting for GPU" to the user instead of leaving the
    job looking stalled.
    """
    if not settings.GPU_LOCK_DSN:
        yield
        return

    try:
        conn = _get_engine().connect()
    except Exception as exc:
        raise Transient(f"GPU mutex database unreachable: {exc}") from exc

    timer: threading.Timer | None = None
    try:
        started = time.monotonic()
        # Try once without blocking so the common (free GPU) path costs nothing
        # and we only report a wait when there really is one.
        try:
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": settings.GPU_LOCK_KEY}
            ).scalar()
            if not got:
                if on_wait is not None:
                    on_wait(0.0)
                log.info("waiting for the GPU (held by another service)")
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": settings.GPU_LOCK_KEY})
            # Recorded before yielding so the watchdog timer measures only the hold.
            backend_pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
        except Exception as exc:
            raise Transient(f"GPU mutex acquisition failed: {exc}") from exc

        waited = time.monotonic() - started
        metrics.GPU_LOCK_WAIT.observe(waited)
        if waited > settings.GPU_LOCK_WARN_SECONDS:
            log.warning("waited %.0fs for the GPU", waited)
        elif waited > 1.0:
            log.info("acquired the GPU after %.1fs", waited)

        # Hard ceiling. A daemon timer, so it can never keep the process alive.
        if backend_pid and settings.WORKER_GPU_LOCK_MAX_HOLD_SECONDS > 0:
            timer = threading.Timer(
                settings.WORKER_GPU_LOCK_MAX_HOLD_SECONDS, _force_release, args=(backend_pid,)
            )
            timer.daemon = True
            timer.start()

        metrics.HELD.acquired()
        try:
            yield
        finally:
            metrics.HELD.released()
            if timer is not None:
                timer.cancel()
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": settings.GPU_LOCK_KEY}
                )
            except Exception as exc:
                # If the unlock itself fails the connection is broken, and closing it
                # below releases the lock anyway. Never mask the job's own outcome.
                log.warning("explicit GPU unlock failed (closing will release it): %s", exc)
    finally:
        # Closing returns the connection to the pool; any lock still held would
        # be released by the backend on disconnect, but we unlock explicitly
        # above so the pooled connection is reusable.
        conn.close()


# Set when the startup probe fails, so gpu_lock() raises Transient instead of the
# worker discovering the problem per-job as a permanent failure.
_unreachable = False


def check() -> bool:
    """Startup probe: can we reach the lock database at all?

    A False return used to be **ignored** by the caller — the worker started anyway and
    then permanently failed every job on the generic exception from ``gpu_lock()``. Now
    the failure is recorded so acquisition raises ``Transient`` and jobs are retried
    with backoff, which is the right degradation: an unreachable mutex database should
    slow the queue, not empty it.
    """
    global _unreachable
    if not settings.GPU_LOCK_DSN:
        log.info("GPU mutex disabled (GPU_LOCK_DSN unset)")
        return True
    try:
        with _get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        _unreachable = False
        log.info("GPU mutex ready (key=%s)", hex(settings.GPU_LOCK_KEY))
        return True
    except Exception as exc:
        _unreachable = True
        log.error(
            "GPU mutex UNREACHABLE at startup: %s — jobs will be retried rather than "
            "run without the mutex. Running without it risks CUDA OOM in BOTH this "
            "service and DicomSegVR, since GPU time-slicing shares compute, not VRAM.",
            exc,
        )
        return False

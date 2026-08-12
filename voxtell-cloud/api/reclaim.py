"""Reclaiming jobs whose worker died, stalled, or ran too long.

**Why this lives in the API.** Stale-job recovery used to run only inside the
worker's own poll loop, which meant the single failure it existed to handle — the
worker not running — was the one failure it could not handle. With
``replicas: 1`` and ``strategy: Recreate``, a CrashLoopBackOff from a bad image
tag, an unmounted ``/models`` hostPath, a CUDA init failure or an OOMKill leaves
every ``running`` job frozen and every ``queued`` job untouched, indefinitely,
with the user seeing a job stuck at 20 % and no error. The API is
``replicas: 2`` with a rolling strategy, so at least one replica is up across a
rollout — strictly more available than a CronJob, whose granularity floor is 60 s
and which would need its own image, ServiceAccount and RBAC. The worker keeps a
throttled copy so a worker with a dead API still self-heals.

**Two clocks, opposite treatment.** This is the whole design:

``lease_expires_at`` answers "is the worker still making progress?" It is extended
only by *observed* progress, so a job wedged inside the GPU call stops renewing
even though the process is alive and its event loop is fine. Expiry means the
worker is dead or stalled, which is recoverable: requeue, up to
``WORKER_MAX_ATTEMPTS``.

``deadline_at`` answers "has this job had long enough, whatever it is doing?" It
is set once at claim and never extended. Expiry means the job is pathological, and
that is **not** recoverable: fail it terminally *even with attempts left*. A job
that wedges on one particular input wedges again, and each retry spends another
hour of a GPU that is shared with DicomSegVR.

Requeued rows deliberately keep their original ``queued_at``. A job interrupted by
a worker death has already waited, and preserving the timestamp is what lets the
claim's aging clause promote it ahead of newer work rather than sending it to the
back of the line.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import db
from .config import settings
from .metrics import observe_reclaim

log = logging.getLogger(__name__)

# Distinct from _SCHEMA_LOCK_KEY, _SWEEP_LOCK_KEY and the worker's GPU_LOCK_KEY:
# advisory locks share one namespace per database. ASCII "VXRCL".
_RECLAIM_LOCK_KEY = 0x565852434C

# Lease expired, budget left -> back to the queue. worker_id and progress are
# cleared so the row does not claim to be owned by a worker that is gone, but
# queued_at is untouched (see the module docstring) and attempts is untouched
# because the *claim* is what increments it.
_RECLAIM_LEASE_SQL = text(
    """
    UPDATE jobs
       SET state = 'queued',
           worker_id = NULL,
           progress = 0,
           lease_expires_at = NULL,
           deadline_at = NULL,
           message = 'Requeued after the worker stopped responding'
     WHERE state = 'running'
       AND lease_expires_at IS NOT NULL
       AND lease_expires_at < now()
       AND attempts < :max_attempts
 RETURNING id
    """
)

# Lease expired and the budget is spent. Terminal.
_FAIL_ATTEMPTS_EXHAUSTED_SQL = text(
    """
    UPDATE jobs
       SET state = 'failed',
           finished_at = now(),
           failure_class = 'stalled',
           message = 'Failed',
           error = 'The worker stopped responding repeatedly; giving up after '
                   || attempts || ' attempt(s)'
     WHERE state = 'running'
       AND lease_expires_at IS NOT NULL
       AND lease_expires_at < now()
       AND attempts >= :max_attempts
 RETURNING id
    """
)

# Wall clock exceeded. Terminal regardless of attempts -- see the docstring.
_FAIL_DEADLINE_SQL = text(
    """
    UPDATE jobs
       SET state = 'failed',
           finished_at = now(),
           failure_class = 'timeout',
           message = 'Failed',
           error = 'Exceeded the maximum run time; the job was stopped'
     WHERE state = 'running'
       AND deadline_at IS NOT NULL
       AND deadline_at < now()
 RETURNING id
    """
)


async def _try_lock(session: AsyncSession) -> bool:
    got = await session.scalar(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": _RECLAIM_LOCK_KEY}
    )
    return bool(got)


async def _unlock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _RECLAIM_LOCK_KEY})


async def reclaim_once() -> dict[str, int]:
    """One pass. Returns a count per action, for logging and metrics."""
    actions = {"lease_expired_requeued": 0, "attempts_exhausted_failed": 0, "deadline_failed": 0}

    # Accessed through the module, not imported by name: a `from .db import
    # SessionLocal` would bind the production engine at import time and be immune to
    # rebinding, which is the same trap worker/db.py documents.
    async with db.SessionLocal() as session:
        if not await _try_lock(session):
            return actions
        try:
            params = {"max_attempts": settings.WORKER_MAX_ATTEMPTS}

            # Deadline first: a job that is both over its deadline and past its
            # lease must fail terminally, not go round again. Running this before
            # the requeue makes that ordering explicit rather than incidental.
            failed_deadline = (await session.execute(_FAIL_DEADLINE_SQL)).fetchall()
            exhausted = (await session.execute(_FAIL_ATTEMPTS_EXHAUSTED_SQL, params)).fetchall()
            requeued = (await session.execute(_RECLAIM_LEASE_SQL, params)).fetchall()
            await session.commit()

            actions["deadline_failed"] = len(failed_deadline)
            actions["attempts_exhausted_failed"] = len(exhausted)
            actions["lease_expired_requeued"] = len(requeued)
            observe_reclaim(actions)

            if requeued:
                log.warning(
                    "reclaimed %d job(s) whose lease expired: %s",
                    len(requeued), [str(r[0]) for r in requeued],
                )
            if exhausted:
                log.error(
                    "terminally failed %d job(s) after repeated worker loss: %s",
                    len(exhausted), [str(r[0]) for r in exhausted],
                )
            if failed_deadline:
                log.error(
                    "terminally failed %d job(s) that exceeded their deadline: %s",
                    len(failed_deadline), [str(r[0]) for r in failed_deadline],
                )
        except Exception:
            await session.rollback()
            raise
        finally:
            await _unlock(session)
            await session.commit()

    return actions


async def reclaim_loop() -> None:
    """Run forever. Sleeps at the END so a restart reclaims immediately."""
    while True:
        try:
            await reclaim_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("reclaim pass failed: %s", exc)
        await asyncio.sleep(settings.VOXTELL_RECLAIM_INTERVAL_SECONDS)

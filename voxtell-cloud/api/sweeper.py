"""Background retention sweep.

Four jobs, all of them about not keeping patient data longer than needed:

1. **Abandoned uploads** — a job left in ``awaiting_upload``, or a volume left in
   ``uploading``, past ``UPLOAD_TTL_MINUTES`` is failed and its multipart upload
   aborted, so SeaweedFS does not accumulate orphaned parts nobody will ever
   complete.
2. **Expired results** — contours and masks are deleted ``RESULT_TTL_HOURS``
   after a job finishes; the row survives (state ``expired``) so the user still
   sees the job happened.
3. **Legacy job volumes** — a job that uploaded its own volume inline still owns
   it, so that object is deleted as soon as the job reaches a terminal state.
   Nothing needs the CT once the masks exist.
4. **Expired shared volumes** — a volume from ``POST /v1/volumes`` is *not* tied
   to any one job, because its whole purpose is to outlive one so a planner can
   try another prompt without re-uploading. It goes when its sliding TTL runs out,
   or when its hard age ceiling does. See ``retention.py`` for the policy and the
   privacy reasoning behind those two numbers.

Item 3 used to read "the input volume is deleted as soon as a job reaches a
terminal state" with no qualification, and that is now only true for the legacy
path — hence ``Job.volume_id IS NULL`` in its query, and the rename. A shared
volume must survive its jobs. The interlock in item 4 is expressed as a query
against ``jobs`` rather than a stored refcount: a counter drifts when a pod dies
mid-job, and a wrong refcount here deletes a patient's CT out from under a
running segmentation.

All API replicas run this loop; every step is idempotent and guarded by a
Postgres advisory lock so only one replica sweeps at a time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import db, storage
from .config import settings
from .models import Job, Volume, utcnow

log = logging.getLogger(__name__)

# Arbitrary but fixed key, distinct from the GPU mutex the workers use.
_SWEEP_LOCK_KEY = 0x565853574
_TERMINAL = ("done", "failed", "cancelled")


async def _try_lock(session: AsyncSession) -> bool:
    """Non-blocking advisory lock: whoever gets it sweeps, the others skip."""
    got = await session.scalar(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": _SWEEP_LOCK_KEY}
    )
    return bool(got)


async def _unlock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _SWEEP_LOCK_KEY})


async def _abandon_uploads(session: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(minutes=settings.VOXTELL_UPLOAD_TTL_MINUTES)
    result = await session.execute(
        select(Job).where(Job.state == "awaiting_upload", Job.created_at < cutoff)
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        if job.upload_id:
            await storage.abort_multipart(job.volume_key, job.upload_id)
            job.upload_id = None
        await storage.delete_prefix(storage.job_prefix(job.user_id, job.id))
        job.state = "failed"
        job.error = "Upload never completed"
        job.finished_at = utcnow()
    return len(jobs)


async def _abandon_volume_uploads(session: AsyncSession) -> int:
    """Same treatment for a volume whose client vanished mid-upload.

    The row is deleted rather than left in ``failed``: nothing references it (a
    job cannot be created against a volume that never became ready), and the
    partial unique index means a clean re-upload of the same series would be
    blocked by a lingering non-failed row.
    """
    cutoff = utcnow() - timedelta(minutes=settings.VOXTELL_UPLOAD_TTL_MINUTES)
    result = await session.execute(
        select(Volume).where(Volume.state == "uploading", Volume.created_at < cutoff)
    )
    volumes = list(result.scalars().all())
    for volume in volumes:
        if volume.upload_id:
            await storage.abort_multipart(volume.object_key, volume.upload_id)
            volume.upload_id = None
        await _delete_volume_object(session, volume)
        await session.delete(volume)
    return len(volumes)


async def _purge_legacy_job_volumes(session: AsyncSession) -> int:
    """Drop the input CT once a job is terminal — the results are enough.

    **Legacy inline uploads only** — hence ``Job.volume_id IS NULL``. A job created
    against a shared volume does not own its input and must not delete it; that
    object may be feeding the next prompt the planner tries, and is retired by
    :func:`_expire_volumes` on its own schedule instead.

    The worker already deletes the volume when it finishes a legacy job, so this
    only catches the ones it never saw: cancelled before it started, or failed at
    submit. Scoped to jobs that finished inside the last few sweep windows —
    without that bound this would re-HEAD every terminal job the deployment has
    ever run, on every pass, forever.
    """
    horizon = utcnow() - timedelta(
        seconds=settings.VOXTELL_SWEEP_INTERVAL_SECONDS * 4
    )
    result = await session.execute(
        select(Job).where(
            Job.state.in_(_TERMINAL),
            Job.finished_at.is_not(None),
            Job.finished_at >= horizon,
            Job.purged_at.is_(None),
            Job.volume_id.is_(None),
        )
    )
    purged = 0
    for job in result.scalars().all():
        if await storage.object_exists(job.volume_key):
            await storage.delete_prefix(job.volume_key)
            purged += 1
    return purged


async def _delete_volume_object(session: AsyncSession, volume: Volume) -> None:
    """Delete the S3 object unless a *different* live row shares the key.

    Keys are content-addressed, so two rows with identical bytes but different
    geometry legitimately point at one object.
    """
    others = await session.scalar(
        select(func.count(Volume.id)).where(
            Volume.object_key == volume.object_key,
            Volume.id != volume.id,
            Volume.state.in_(("uploading", "ready")),
        )
    )
    if int(others or 0) == 0:
        await storage.delete_prefix(volume.object_key)


async def _expire_volumes(session: AsyncSession) -> int:
    """Retire shared volumes past their expiry.

    The interlock — never while a job is queued or running against it — is a live
    query, not a stored count. If a volume is still busy the sweep simply leaves
    it; the next pass reconsiders, and the hard age ceiling in ``retention.py``
    guarantees it cannot be deferred forever.
    """
    now = utcnow()
    result = await session.execute(
        select(Volume).where(Volume.expires_at <= now)
    )
    expired = 0
    for volume in result.scalars().all():
        busy = await session.scalar(
            select(func.count(Job.id)).where(
                Job.volume_id == volume.id, Job.state.in_(("queued", "running"))
            )
        )
        if int(busy or 0) > 0:
            continue

        if volume.upload_id:
            await storage.abort_multipart(volume.object_key, volume.upload_id)
            volume.upload_id = None
        await _delete_volume_object(session, volume)
        await session.delete(volume)
        expired += 1
    return expired


async def _expire_results(session: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(hours=settings.VOXTELL_RESULT_TTL_HOURS)
    result = await session.execute(
        select(Job).where(
            Job.state.in_(_TERMINAL),
            Job.finished_at.is_not(None),
            Job.finished_at < cutoff,
            Job.purged_at.is_(None),
        )
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        await storage.delete_prefix(storage.job_prefix(job.user_id, job.id))
        job.result_key = None
        job.mask_key = None
        job.purged_at = utcnow()
        if job.state == "done":
            job.state = "expired"
            job.message = "Result expired and was deleted"
    return len(jobs)


async def sweep_once() -> None:
    # Through the module, not by name — see the note in reclaim.py.
    async with db.SessionLocal() as session:
        if not await _try_lock(session):
            return
        try:
            abandoned = await _abandon_uploads(session)
            abandoned += await _abandon_volume_uploads(session)
            legacy = await _purge_legacy_job_volumes(session)
            retired = await _expire_volumes(session)
            expired = await _expire_results(session)
            await session.commit()
            if abandoned or legacy or retired or expired:
                log.info(
                    "sweep: %d abandoned upload(s), %d legacy volume(s) purged, "
                    "%d shared volume(s) retired, %d result(s) expired",
                    abandoned, legacy, retired, expired,
                )
        except Exception:
            await session.rollback()
            raise
        finally:
            await _unlock(session)
            await session.commit()


async def retention_loop() -> None:
    """Run forever; a failed pass is logged and retried next interval.

    Sleeps at the **end**, not the start. It used to sleep first, which meant every
    API restart bought fifteen minutes with no retention at all — so a rollout
    during a busy period left abandoned uploads holding multipart state and expired
    results sitting in the bucket for a quarter of an hour longer than the policy
    claims. Doing a pass immediately also surfaces a broken sweeper in the startup
    logs instead of a quarter-hour later.
    """
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sweep failed: %s", exc)
        await asyncio.sleep(settings.VOXTELL_SWEEP_INTERVAL_SECONDS)


# Kept so an in-flight rollout or a stale import does not break; retention_loop is
# the name to use.
sweep_loop = retention_loop

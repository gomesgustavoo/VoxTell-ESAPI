"""Reusable volumes: upload a series once, segment it many times.

Why this is a resource and not a cache
--------------------------------------
The alternative designs both failed on the product requirement rather than on
elegance.

*Reuse from a previous job* (``reuse_volume_from: <job_id>``) needs that first job
to exist, so the first upload is still a job — which books a quota unit and an
outstanding slot, which means "Upload series" cannot be a step separate from
running a segmentation. It also couples the object's lifetime to a job prefix
that four different code paths delete.

*An implicit content-addressed cache* keeps the upload inside the quota-consuming
call, so a failed upload still costs a unit. And it gives the UI no handle: no
expiry to display, no way to say "your upload lapsed", no way to release early.

An explicit resource gives the operator something nameable, pollable and
deletable — and the content hash gives it the one property the cache had for
free: :func:`create_volume` is **idempotent**, so unlike ``POST /v1/jobs`` it is
safe to retry.

Concurrency
-----------
The user row is locked while the dedup lookup and the cap check run, so two
simultaneous identical POSTs cannot both take the last slot. The lock is
deliberately *released before* the S3 round trip — holding a row lock across
``create_multipart`` + ``presign_parts`` would serialise every upload in the
deployment behind one user's network latency. A crash in the gap leaves a row
with ``upload_id IS NULL``, and a retry of the same request finds it and fills it
in, which is the same code path as the idempotent hit.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from voxtell_cloud.geometry import build_affine_lps
from voxtell_cloud.wire import MAX_PARTS, PART_SIZE, geometry_sha256, part_count

from .. import storage
from ..auth import get_caller
from ..config import settings
from ..db import get_session
from ..errors import (
    bad_request,
    conflict,
    not_found,
    payload_too_large,
    quota_exceeded,
)
from ..models import Job, User, Volume, utcnow
from ..quota import load_state
from ..retention import next_expiry
from ..schemas import (
    UploadPart,
    VolumeCompleteRequest,
    VolumeCreatedResponse,
    VolumeCreateRequest,
    VolumeListResponse,
    VolumeResponse,
)

router = APIRouter(prefix="/volumes", tags=["volumes"])

# A volume in one of these still occupies one of the user's slots.
LIVE_STATES = ("uploading", "ready")


def _require_enabled() -> None:
    """404 when the feature is off, so a probing client falls back cleanly.

    404 rather than 501: to a client, "this deployment does not have volumes" and
    "this API is too old to have volumes" are the same situation and want the same
    response. ``/v1/me``'s capability list is the supported way to ask.
    """
    if not settings.VOXTELL_VOLUMES_ENABLED:
        raise not_found("Volumes are not enabled on this deployment")


async def _load_owned_volume(
    session: AsyncSession, user: User, volume_id: str, *, for_update: bool = False
) -> Volume:
    """Load a volume scoped to the caller. 404 if missing or someone else's."""
    try:
        vid = uuid.UUID(volume_id)
    except (ValueError, AttributeError):
        # Unparseable id is a 404, not a 422: never leak whether an id exists.
        raise not_found("Volume not found")

    stmt = select(Volume).where(Volume.id == vid, Volume.user_id == user.id)
    if for_update:
        stmt = stmt.with_for_update()
    volume = (await session.execute(stmt)).scalar_one_or_none()
    if volume is None:
        raise not_found("Volume not found")
    return volume


def _to_response(volume: Volume) -> VolumeResponse:
    geom = volume.geometry or {}
    return VolumeResponse(
        volume_id=volume.id,
        state=volume.state,
        content_sha256=volume.content_sha256,
        bytes=volume.bytes,
        voxels=volume.voxels,
        x_size=int(geom.get("x_size", 0)),
        y_size=int(geom.get("y_size", 0)),
        z_size=int(geom.get("z_size", 0)),
        jobs_run=volume.jobs_run,
        created_at=volume.created_at,
        expires_at=volume.expires_at,
    )


async def _presign_and_attach(session: AsyncSession, volume: Volume) -> list[UploadPart]:
    """Open a multipart upload for ``volume`` and hand back its part URLs.

    Called outside the user-row lock. Re-presigning an upload that is already open
    is fine and is what makes a retry work: S3 allows a part to be re-PUT, so
    reissuing the whole list simply lets the client redo whatever it had done.
    """
    parts = part_count(volume.bytes)

    if not volume.upload_id:
        volume.upload_id = await storage.create_multipart(volume.object_key)
        await session.commit()

    urls = await storage.presign_parts(volume.object_key, volume.upload_id, parts)
    return [UploadPart(part_number=i + 1, url=u) for i, u in enumerate(urls)]


# --------------------------------------------------------------------------- #
# Create (idempotent)
# --------------------------------------------------------------------------- #
@router.post("", response_model=VolumeCreatedResponse)
async def create_volume(
    body: VolumeCreateRequest,
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> VolumeCreatedResponse:
    """Reserve a volume and hand back presigned upload URLs.

    Returns 200 with ``upload: []`` and ``reused: true`` when this exact series is
    already held — which is what makes re-opening the plugin on the same patient
    cost nothing.

    Books **no quota and no outstanding slot**: uploading is not GPU work. That is
    the fix for a real lockout — ``awaiting_upload`` used to count against the
    six-job cap and was only reaped after 120 minutes, so six failed uploads
    inside two hours returned 429 with a ``Retry-After: 30`` that was wrong by two
    orders of magnitude, *and* burned six of the 200 monthly units before a single
    byte had been transferred.
    """
    _require_enabled()

    geom = body.geometry

    if geom.voxels > settings.VOXTELL_MAX_VOXELS:
        raise payload_too_large(
            "volume_too_large",
            f"{geom.voxels} voxels exceeds the {settings.VOXTELL_MAX_VOXELS} limit.",
        )
    if body.upload_bytes > settings.VOXTELL_MAX_UPLOAD_BYTES:
        raise payload_too_large(
            "upload_too_large",
            f"{body.upload_bytes} bytes exceeds the "
            f"{settings.VOXTELL_MAX_UPLOAD_BYTES} limit.",
        )
    # A gzip stream can never be larger than the raw data plus a small overhead;
    # anything bigger means the client mislabelled the payload (e.g. sent int32).
    raw_bytes = geom.voxels * 2
    if body.upload_bytes > raw_bytes + 1024 * 1024:
        raise bad_request(
            {
                "error": "upload_bytes_implausible",
                "message": (
                    f"{body.upload_bytes} compressed bytes exceeds the {raw_bytes}-byte "
                    "uncompressed size of this geometry — is the volume int16?"
                ),
            }
        )
    parts = part_count(body.upload_bytes)
    if parts > MAX_PARTS:
        raise payload_too_large(
            "too_many_parts", f"{parts} parts exceeds the {MAX_PARTS} limit."
        )

    # Non-binding, read-only: there is no point uploading 34 MB when the month is
    # already spent. Deliberately NOT admit() — this takes no lock and reserves
    # nothing, so it cannot deny a slot. The binding check happens at job submit.
    #
    # Reuses the same 402 the job routes raise, so the client's existing quota
    # handler covers this with no change.
    state = await load_state(session, user)
    if state.limit is not None and state.used >= state.limit:
        raise quota_exceeded(used=state.used, limit=state.limit)

    geom_hash = geometry_sha256(geom.model_dump())
    now = utcnow()

    # --- short transaction: lock the user row, dedup, cap, insert ---
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())

    # Dedup BEFORE the cap check. The other order breaks idempotency: a retry of a
    # request that already succeeded would be refused for exceeding the cap its
    # own first attempt filled.
    existing = (
        await session.execute(
            select(Volume).where(
                Volume.user_id == user.id,
                Volume.content_sha256 == body.content_sha256,
                Volume.geometry_sha256 == geom_hash,
                Volume.state != "failed",
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.expires_at = next_expiry(existing.created_at, now)
        existing.last_used_at = now
        await session.commit()

        if existing.state == "ready":
            return VolumeCreatedResponse(
                volume_id=existing.id,
                state=existing.state,
                content_sha256=existing.content_sha256,
                upload=[],
                part_size=PART_SIZE,
                expires_in=settings.VOXTELL_PRESIGN_EXPIRY_SECONDS,
                expires_at=existing.expires_at,
                reused=True,
            )

        # An interrupted upload. Same volume_id, a fresh set of part URLs.
        return VolumeCreatedResponse(
            volume_id=existing.id,
            state=existing.state,
            content_sha256=existing.content_sha256,
            upload=await _presign_and_attach(session, existing),
            part_size=PART_SIZE,
            expires_in=settings.VOXTELL_PRESIGN_EXPIRY_SECONDS,
            expires_at=existing.expires_at,
            reused=True,
        )

    live = await session.scalar(
        select(func.count(Volume.id)).where(
            Volume.user_id == user.id, Volume.state.in_(LIVE_STATES)
        )
    )
    if int(live or 0) >= settings.VOXTELL_MAX_VOLUMES_PER_USER:
        oldest = (
            await session.execute(
                select(Volume)
                .where(Volume.user_id == user.id, Volume.state.in_(LIVE_STATES))
                .order_by(Volume.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        # 409 not 429: waiting does not help, releasing does. Name the volume to
        # release rather than making the caller list them to find out.
        raise conflict(
            "too_many_volumes",
            f"You are holding {live} uploaded series (limit "
            f"{settings.VOXTELL_MAX_VOLUMES_PER_USER}). Release one first"
            + (f", e.g. {oldest.id}." if oldest is not None else "."),
        )

    affine_lps = build_affine_lps(
        row_direction=geom.row_direction,
        col_direction=geom.col_direction,
        slice_direction=geom.slice_direction,
        x_res=geom.x_res,
        y_res=geom.y_res,
        z_res=geom.z_res,
        origin=geom.origin,
    ).tolist()

    volume = Volume(
        id=uuid.uuid4(),
        user_id=user.id,
        state="uploading",
        content_sha256=body.content_sha256,
        geometry_sha256=geom_hash,
        # content_sha256 rides along in the JSONB so the worker can verify the
        # decoded bytes without a schema change of its own.
        geometry={
            **geom.model_dump(),
            "affine_lps": affine_lps,
            "content_sha256": body.content_sha256,
        },
        object_key=storage.shared_volume_key(user.id, body.content_sha256),
        upload_id=None,
        bytes=body.upload_bytes,
        voxels=geom.voxels,
        created_at=now,
        expires_at=next_expiry(now, now),
    )
    session.add(volume)

    try:
        await session.commit()
    except IntegrityError:
        # A concurrent identical POST won the unique index. Re-read and serve the
        # winner — same outcome the caller would have got by arriving second.
        await session.rollback()
        winner = (
            await session.execute(
                select(Volume).where(
                    Volume.user_id == user.id,
                    Volume.content_sha256 == body.content_sha256,
                    Volume.geometry_sha256 == geom_hash,
                    Volume.state != "failed",
                )
            )
        ).scalar_one_or_none()
        if winner is None:
            raise conflict(
                "volume_create_conflict",
                "The volume could not be created; retry the request.",
            )
        return VolumeCreatedResponse(
            volume_id=winner.id,
            state=winner.state,
            content_sha256=winner.content_sha256,
            upload=[] if winner.state == "ready" else await _presign_and_attach(session, winner),
            part_size=PART_SIZE,
            expires_in=settings.VOXTELL_PRESIGN_EXPIRY_SECONDS,
            expires_at=winner.expires_at,
            reused=True,
        )

    # --- outside the lock: the S3 round trip ---
    upload = await _presign_and_attach(session, volume)

    return VolumeCreatedResponse(
        volume_id=volume.id,
        state=volume.state,
        content_sha256=volume.content_sha256,
        upload=upload,
        part_size=PART_SIZE,
        expires_in=settings.VOXTELL_PRESIGN_EXPIRY_SECONDS,
        expires_at=volume.expires_at,
        reused=False,
    )


# --------------------------------------------------------------------------- #
# Complete
# --------------------------------------------------------------------------- #
@router.post("/{volume_id}/complete", response_model=VolumeResponse)
async def complete_volume(
    body: VolumeCompleteRequest,
    volume_id: str = Path(..., description="Volume UUID"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> VolumeResponse:
    """Assemble the multipart upload and mark the volume ready.

    Idempotent: calling it on an already-ready volume returns 200 with the same
    body rather than 409, so a client whose response was lost can simply retry.
    """
    _require_enabled()
    volume = await _load_owned_volume(session, user, volume_id, for_update=True)

    if volume.state == "ready":
        return _to_response(volume)
    if volume.state == "failed":
        raise conflict("volume_failed", "This upload failed; create a new volume.")
    if not volume.upload_id:
        raise conflict("no_upload_in_progress", "This volume has no open upload.")

    expected = part_count(volume.bytes)
    if len(body.parts) != expected:
        raise bad_request(
            {
                "error": "part_count_mismatch",
                "message": f"Expected {expected} part(s), got {len(body.parts)}.",
            }
        )

    try:
        size = await storage.complete_multipart(
            volume.object_key, volume.upload_id, [p.model_dump() for p in body.parts]
        )
    except Exception as exc:
        raise bad_request({"error": "upload_incomplete", "message": str(exc)})

    if size != volume.bytes:
        # Cheap tripwire before any GPU time: the assembled object disagrees with
        # what was declared. Fail the row rather than deleting it, so the partial
        # upload cannot be mistaken for a usable volume, and the partial index
        # lets a clean re-upload of the same series take its place.
        await _purge_object_if_unreferenced(session, volume)
        volume.state = "failed"
        volume.upload_id = None
        await session.commit()
        raise bad_request(
            {
                "error": "upload_size_mismatch",
                "message": f"uploaded {size} bytes, expected {volume.bytes}",
            }
        )

    now = utcnow()
    volume.upload_id = None
    volume.state = "ready"
    volume.ready_at = now
    volume.last_used_at = now
    volume.expires_at = next_expiry(volume.created_at, now)
    # Commit before responding: the client's very next call creates a job against
    # this id, often on the other replica.
    await session.commit()
    return _to_response(volume)


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
@router.get("/{volume_id}", response_model=VolumeResponse)
async def get_volume(
    volume_id: str = Path(..., description="Volume UUID"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> VolumeResponse:
    """State and expiry. Polling this also slides the TTL — a client still asking
    is a client still working."""
    _require_enabled()
    volume = await _load_owned_volume(session, user, volume_id)

    if volume.state == "ready":
        now = utcnow()
        volume.last_used_at = now
        volume.expires_at = next_expiry(volume.created_at, now)
        await session.commit()

    return _to_response(volume)


@router.get("", response_model=VolumeListResponse)
async def list_volumes(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> VolumeListResponse:
    """What patient data this account currently holds.

    Half of the privacy argument for keeping volumes at all: retention is bounded
    *and* visible, so it can be audited and purged rather than merely trusted.
    """
    _require_enabled()
    result = await session.execute(
        select(Volume)
        .where(Volume.user_id == user.id)
        .order_by(Volume.created_at.desc())
        .limit(limit)
    )
    return VolumeListResponse(
        volumes=[_to_response(v) for v in result.scalars().all()]
    )


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #
async def _purge_object_if_unreferenced(session: AsyncSession, volume: Volume) -> None:
    """Delete the S3 object unless another live row shares the key.

    Keys are content-addressed, so two rows with identical bytes but different
    geometry legitimately point at one object. Releasing one must not pull the
    bytes out from under the other.
    """
    if volume.upload_id:
        await storage.abort_multipart(volume.object_key, volume.upload_id)

    others = await session.scalar(
        select(func.count(Volume.id)).where(
            Volume.object_key == volume.object_key,
            Volume.id != volume.id,
            Volume.state.in_(LIVE_STATES),
        )
    )
    if int(others or 0) == 0:
        await storage.delete_prefix(volume.object_key)


@router.delete("/{volume_id}", status_code=204)
async def delete_volume(
    volume_id: str = Path(..., description="Volume UUID"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Release an uploaded series now, before its TTL.

    The lever the privacy posture depends on, surfaced in the plugin as "Remove
    series" and called on sign-out. Refused while a job is still queued or running
    against it — that job would fail on a missing input, and reporting the refusal
    is more useful than producing a confusing downstream failure.

    Deletes the row rather than tombstoning it. A volume has no user-visible
    history worth keeping (``usage_events`` holds the billing record), and dropping
    the row removes the PHI-adjacent geometry along with the bytes. Jobs that ran
    against it keep their own copy of the geometry and their ``volume_key``, so
    their history stays intact.
    """
    _require_enabled()
    volume = await _load_owned_volume(session, user, volume_id, for_update=True)

    busy = await session.scalar(
        select(func.count(Job.id)).where(
            Job.volume_id == volume.id, Job.state.in_(("queued", "running"))
        )
    )
    if int(busy or 0) > 0:
        raise conflict(
            "volume_in_use",
            f"{busy} job(s) are still queued or running against this series. "
            "Cancel them first, or wait for them to finish.",
        )

    await _purge_object_if_unreferenced(session, volume)
    await session.delete(volume)
    await session.commit()

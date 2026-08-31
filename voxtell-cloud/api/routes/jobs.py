"""The job lifecycle: create -> upload -> submit -> poll -> result.

The volume never passes through this process. The client PUTs it straight to
SeaweedFS with presigned multipart URLs and the worker reads it from there, so
the API stays a small stateless control plane and no single HTTP request
approaches Cloudflare's 100 MB body cap.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from voxtell_cloud.geometry import build_affine_lps
from voxtell_cloud.wire import MAX_PARTS, PART_SIZE, part_count

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
    service_unavailable,
)
from ..estimate import wait_estimate_seconds
from ..models import Job, UsageEvent, User, Volume, utcnow
from ..quota import admit, load_state
from ..retention import next_expiry
from ..schemas import (
    JobCreatedResponse,
    JobCreateRequest,
    JobListResponse,
    JobStatusResponse,
    JobSubmitRequest,
    UploadPart,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# States from which a cancel is meaningful.
_CANCELLABLE = ("awaiting_upload", "queued", "running")


async def _load_owned_job(
    session: AsyncSession, user: User, job_id: str, *, for_update: bool = False
) -> Job:
    """Load a job scoped to the caller. 404 if missing or owned by someone else.

    ``for_update`` takes a row lock, and every route that *branches on state and
    then mutates* must use it. Without the lock the API races the worker's
    ``FOR UPDATE SKIP LOCKED`` claim: cancelling a job the worker is picking up at
    that instant read ``queued``, deleted the volume, and wrote ``cancelled`` —
    while the claim wrote ``running``. The worker then ran a job whose input was
    gone and reported a 404 failure instead of a clean cancellation. Observed in
    testing on 2026-08-03.

    With the lock the two orderings are both correct: if the worker claimed first
    we block, then re-read ``running`` and only set the cancel flag; if we lock
    first, the worker's SKIP LOCKED passes the row over and we cancel it outright.
    """
    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        # Unparseable id is a 404, not a 422: never leak whether an id exists.
        raise not_found("Job not found")

    stmt = select(Job).where(Job.id == jid, Job.user_id == user.id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()
    if job is None:
        raise not_found("Job not found")
    return job


async def _queue_position(session: AsyncSession, job: Job) -> int | None:
    """How many queued jobs sit ahead of this one, across all users.

    Deliberately **global**, not scoped to the caller. There is one GPU, so the
    global backlog *is* the planner's real wait; a per-user number would read as
    "you are next" while the job sits behind a dozen others, which is a worse
    disclosure problem than the one it solves. What a shared-GPU service owes its
    users is an honest queue depth.

    The ordering key matches the worker's claim (``COALESCE(queued_at, created_at)``)
    so the number cannot disagree with the dispatch order. It does *not* model the
    fair-share rank — an exact position under round-robin would require replaying
    the whole ordering, and ``estimated_wait_seconds`` is the number clients should
    actually show.
    """
    if job.state != "queued":
        return None
    own_key = func.coalesce(job.queued_at, job.created_at)
    ahead = await session.scalar(
        select(func.count(Job.id)).where(
            Job.state == "queued",
            func.coalesce(Job.queued_at, Job.created_at) < own_key,
        )
    )
    return int(ahead or 0)


async def _queue_positions(session: AsyncSession, jobs: list[Job]) -> dict[uuid.UUID, int]:
    """Queue positions for a whole page of jobs in ONE query.

    ``_queue_position`` runs a COUNT per job, which is fine for a single status poll
    and quadratic-ish for a list: the console asks for 50 and got 50 COUNTs plus 50
    estimate calls on every refresh. Same semantics as the single-job version — the
    number of queued jobs whose ordering key is strictly smaller, ties included —
    computed here from one ordered fetch of the queued set.

    Fetching every queued row is bounded: ``VOXTELL_MAX_GLOBAL_QUEUED`` caps the
    backlog, and ``ix_jobs_dispatch`` is partial on ``state = 'queued'``.
    """
    wanted = [j for j in jobs if j.state == "queued"]
    if not wanted:
        return {}

    key = func.coalesce(Job.queued_at, Job.created_at)
    rows = (
        await session.execute(select(Job.id, key).where(Job.state == "queued").order_by(key))
    ).all()

    keys = [r[1] for r in rows]
    out: dict[uuid.UUID, int] = {}
    own = {r[0]: r[1] for r in rows}
    for job in wanted:
        mine = own.get(job.id)
        if mine is None:
            # Raced: it left the queue between the page fetch and this query.
            continue
        out[job.id] = sum(1 for k in keys if k < mine)
    return out


def _duration_seconds(job: Job) -> float | None:
    """Wall clock on the worker, computed server-side.

    Server-side so every client agrees and none of them has to subtract two
    timestamps in the browser's local timezone — which is how a duration ends up
    an hour out twice a year.
    """
    if job.started_at is None or job.finished_at is None:
        return None
    return round((job.finished_at - job.started_at).total_seconds(), 2)


def _target_count(prompts, structure_ids) -> int:
    """How many things a job asked for, whichever way it was addressed.

    The usage ledger's column is called ``prompts`` for historical reasons, and
    feeding it ``len(body.prompts)`` recorded **zero** for a catalog-addressed job.
    A CADS job asking for forty structures would have metered as nothing, which is
    a billing and capacity-planning error rather than a cosmetic one.
    """
    return len(prompts or []) or len(structure_ids or [])


async def _status(
    session: AsyncSession,
    job: Job,
    position: int | None = None,
    *,
    position_known: bool = False,
) -> JobStatusResponse:
    """Build a status payload.

    ``position_known`` distinguishes "the caller already looked it up and the answer
    was None" from "the caller did not look". Without it, a batched list would
    silently re-run the per-job COUNT for every job whose position is legitimately
    null — which is every non-queued job, i.e. almost all of them.
    """
    if not position_known:
        position = await _queue_position(session, job)
    return JobStatusResponse(
        job_id=job.id,
        state=job.state,
        progress=job.progress,
        message=job.message,
        error=job.error,
        prompts=list(job.prompts or []),
        structure_ids=list(job.structure_ids or []),
        models=list(job.models or []),
        queue_position=position,
        # Derived from measured throughput rather than a guess. "About 4 minutes" is
        # something a planner can act on; "6 jobs ahead" is not.
        estimated_wait_seconds=(
            await wait_estimate_seconds(session, position) if position is not None else None
        ),
        poll_after=settings.VOXTELL_POLL_INTERVAL_SECONDS,
        has_mask=bool(job.mask_key),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        # Additive fields for the dashboard; all existing columns.
        queued_at=job.queued_at,
        duration_seconds=_duration_seconds(job),
        gpu_seconds=job.gpu_seconds,
        voxels=job.voxels,
        bytes_in=job.bytes_in,
        attempts=job.attempts,
        failure_class=job.failure_class,
        volume_id=job.volume_id,
    )


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
@router.post("", response_model=JobCreatedResponse, status_code=201)
async def create_job(
    body: JobCreateRequest,
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> JobCreatedResponse:
    """Reserve a job. Two shapes — see :class:`JobCreateRequest`.

    With ``volume_id`` the job is created straight into ``queued`` and there is no
    upload step, so the response carries an empty ``upload`` list. With
    ``geometry`` + ``upload_bytes`` this behaves exactly as it always has.
    """
    # Refuse structure-addressed work this deployment cannot actually run, before a
    # GPU slot is committed. The catalog is served whether or not the weights are
    # deployed — the plugin needs it to render its picker at all — so without this
    # guard a planner picks a protocol, waits for an upload and a queue position, and
    # gets a job that completed with nothing in it. Naming the models makes it
    # actionable rather than mysterious.
    if body.structure_ids and not settings.VOXTELL_CATALOG_MODELS_ENABLED:
        raise service_unavailable(
            "catalog_models_unavailable",
            "This deployment cannot run catalog models yet ("
            + ", ".join(body.resolved_models)
            + "). Free-text prompts work; ask the administrator to deploy the "
            "weights for these models.",
        )

    if body.volume_id is not None:
        return await _create_job_from_volume(body, user, session)
    return await _create_job_inline(body, user, session)


async def _create_job_from_volume(
    body: JobCreateRequest, user: User, session: AsyncSession
) -> JobCreatedResponse:
    """Queue a job against an already-uploaded volume.

    Everything ``awaiting_upload`` existed to police — geometry limits, byte
    plausibility, part counts — was already checked when the volume was created,
    so this state is skipped entirely rather than passed through.
    """
    if not settings.VOXTELL_VOLUMES_ENABLED:
        raise bad_request(
            {
                "error": "volumes_not_enabled",
                "message": "This deployment does not accept volume_id; upload inline instead.",
            }
        )

    volume = (
        await session.execute(
            select(Volume)
            .where(Volume.id == body.volume_id, Volume.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if volume is None:
        # The TTL may simply have passed. Say so in terms the client can act on:
        # its handler re-uploads rather than surfacing a bare 404.
        raise not_found(
            "That uploaded series is no longer held. Upload it again."
        )
    if volume.state != "ready":
        raise conflict(
            "volume_not_ready",
            f"The series is {volume.state}; finish the upload before segmenting.",
        )

    # Binding admission check: this is the moment GPU work is committed. Its
    # returned state carries this user's current queue depth, taken under the
    # user-row lock, which is the new job's fair_rank.
    state = await admit(session, user)

    now = utcnow()
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        user_id=user.id,
        state="queued",
        # This user's Nth waiting job. Ordering by this before the timestamp is what
        # stops one tenant holding every queue position — see models.Job.fair_rank.
        fair_rank=state.queued,
        prompts=body.prompts,
        structure_ids=body.structure_ids,
        # Derived here, not sent by the client: asking for one brain structure and
        # one liver structure must load two networks, and letting the workstation
        # also name a model invites a request whose model and structures disagree.
        models=body.resolved_models if body.structure_ids else [],
        # Copied, not referenced: a job row must stay self-describing after the
        # volume is released, and the worker must not need a join.
        geometry=dict(volume.geometry),
        keep_largest=body.keep_largest,
        want_mask=body.want_mask,
        volume_id=volume.id,
        volume_key=volume.object_key,
        upload_id=None,
        bytes_in=volume.bytes,
        voxels=volume.voxels,
        queued_at=now,
        message="Queued",
    )
    session.add(job)
    session.add(UsageEvent(
        user_id=user.id, job_id=job_id,
        prompts=_target_count(body.prompts, body.structure_ids),
        voxels=volume.voxels))

    volume.jobs_run += 1
    volume.last_used_at = now
    volume.expires_at = next_expiry(volume.created_at, now)

    # Commit before responding so the worker can claim it the moment we say queued.
    await session.commit()

    return JobCreatedResponse(
        job_id=job_id,
        state=job.state,
        # Empty means "nothing to upload" — the same rule POST /v1/volumes uses.
        upload=[],
        part_size=PART_SIZE,
        expires_in=0,
    )


async def _create_job_inline(
    body: JobCreateRequest, user: User, session: AsyncSession
) -> JobCreatedResponse:
    """The original flow: reserve a job and hand back presigned upload URLs.

    Validation happens *before* a single byte is uploaded — the client learns it
    is over quota or sent an impossible geometry in one small round trip.
    """
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

    # Two checks here, and the distinction matters.
    #
    # Non-binding quota: uploading 34 MB when the month is already spent is a
    # waste of the operator's time, so warn early — but take no lock and reserve
    # nothing. The binding check happens at /submit, where GPU work is actually
    # committed. What that trades away is only this: a user at 199/200 who races
    # two uploads learns at submit rather than at create.
    state = await load_state(session, user)
    if state.limit is not None and state.used >= state.limit:
        raise quota_exceeded(used=state.used, limit=state.limit)

    # Binding, and about STORAGE rather than GPU. With admit() moved to /submit,
    # awaiting_upload no longer consumes a GPU slot, so what needs bounding is the
    # number of open multipart uploads one user can hold. Keeping these two limits
    # separate is the actual bug fix: one counter used to serve both, which is why
    # six failed uploads denied GPU access for the next two hours.
    pending = await session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == user.id, Job.state == "awaiting_upload"
        )
    )
    if int(pending or 0) >= settings.VOXTELL_MAX_AWAITING_UPLOAD_PER_USER:
        raise conflict(
            "too_many_pending_uploads",
            f"You have {pending} upload(s) already open (limit "
            f"{settings.VOXTELL_MAX_AWAITING_UPLOAD_PER_USER}). Finish or cancel one first.",
        )

    # The LPS affine is derived once, here, and stored with the job: the worker
    # then needs no geometry maths, and a job row is self-describing forever.
    affine_lps = build_affine_lps(
        row_direction=geom.row_direction,
        col_direction=geom.col_direction,
        slice_direction=geom.slice_direction,
        x_res=geom.x_res,
        y_res=geom.y_res,
        z_res=geom.z_res,
        origin=geom.origin,
    ).tolist()

    job_id = uuid.uuid4()
    key = storage.volume_key(user.id, job_id)
    upload_id = await storage.create_multipart(key)
    urls = await storage.presign_parts(key, upload_id, parts)

    job = Job(
        id=job_id,
        user_id=user.id,
        state="awaiting_upload",
        prompts=body.prompts,
        structure_ids=body.structure_ids,
        models=body.resolved_models if body.structure_ids else [],
        geometry={**geom.model_dump(), "affine_lps": affine_lps},
        keep_largest=body.keep_largest,
        want_mask=body.want_mask,
        volume_key=key,
        upload_id=upload_id,
        bytes_in=body.upload_bytes,
        voxels=geom.voxels,
        message="Waiting for upload",
    )
    session.add(job)
    # No UsageEvent here. It used to be written at create, i.e. before a single
    # byte had been uploaded, so an upload that failed still consumed one of the
    # 200 monthly units. It is written at /submit now, alongside admit().
    #
    # Commit before responding: the client uses job_id for the very next request.
    # See db.get_session for why the dependency teardown is too late.
    await session.commit()

    return JobCreatedResponse(
        job_id=job_id,
        state=job.state,
        upload=[UploadPart(part_number=i + 1, url=u) for i, u in enumerate(urls)],
        part_size=PART_SIZE,
        expires_in=settings.VOXTELL_PRESIGN_EXPIRY_SECONDS,
    )


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #
@router.post("/{job_id}/submit", response_model=JobStatusResponse)
async def submit_job(
    body: JobSubmitRequest,
    job_id: str = Path(..., description="Job UUID"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> JobStatusResponse:
    """Complete the multipart upload and enqueue the job for the GPU."""
    job = await _load_owned_job(session, user, job_id, for_update=True)
    if job.state != "awaiting_upload":
        raise conflict("job_not_awaiting_upload", f"Job is {job.state}.")
    if not job.upload_id:
        raise conflict("no_upload_in_progress", "This job has no open upload.")

    expected = part_count(job.bytes_in or 0)
    if len(body.parts) != expected:
        raise bad_request(
            {
                "error": "part_count_mismatch",
                "message": f"Expected {expected} part(s), got {len(body.parts)}.",
            }
        )

    try:
        size = await storage.complete_multipart(
            job.volume_key, job.upload_id, [p.model_dump() for p in body.parts]
        )
    except Exception as exc:
        raise bad_request({"error": "upload_incomplete", "message": str(exc)})

    if job.bytes_in and size != job.bytes_in:
        # The assembled object disagrees with what was declared at create time:
        # the worker would decode garbage, so refuse now while it is cheap.
        await storage.delete_prefix(storage.job_prefix(user.id, job.id))
        job.state = "failed"
        job.error = f"uploaded {size} bytes, expected {job.bytes_in}"
        job.finished_at = utcnow()
        await session.commit()
        raise bad_request(
            {"error": "upload_size_mismatch", "message": job.error}
        )

    # The binding admission check, deliberately here rather than at create: this
    # is the moment the job becomes GPU work. Doing it at create meant an upload
    # that never finished still held a slot for two hours and had already spent a
    # quota unit. Inside the same transaction as the state change, so the count and
    # the queueing commit or roll back together.
    state = await admit(session, user)

    job.upload_id = None
    job.state = "queued"
    job.queued_at = utcnow()
    # Assigned here rather than at create, for the same reason admit() is: this is
    # the moment the job joins the queue, so this is when "how many of this user's
    # jobs are already waiting" is the right question. Taken from the state admit()
    # computed under the user-row lock — do not recount, or two concurrent submits
    # can both take the same rank.
    job.fair_rank = state.queued
    job.progress = 0.0
    job.message = "Queued"
    session.add(UsageEvent(
        user_id=user.id, job_id=job.id,
        prompts=_target_count(job.prompts, job.structure_ids),
        voxels=job.voxels))
    # Commit before responding so the worker can claim it the moment we say queued.
    await session.commit()
    return await _status(session, job)


# --------------------------------------------------------------------------- #
# Poll / list / cancel / delete
# --------------------------------------------------------------------------- #
@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: str = Path(..., description="Job UUID"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> JobStatusResponse:
    return await _status(session, await _load_owned_job(session, user, job_id))


@router.get("", response_model=JobListResponse)
async def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    state: str | None = Query(
        None,
        pattern="^(awaiting_upload|queued|running|done|failed|cancelled|expired)$",
        description="Filter to one state. Omit for every state.",
    ),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> JobListResponse:
    """The caller's jobs, newest first.

    The default is deliberately unchanged — no filter, ``limit=50``, ``offset=0`` —
    because the approved 2.0.1.0 plugin calls this with no parameters and its job
    list must not silently shrink or reorder. ``state`` and ``offset`` are opt-in,
    and ``total`` is additive.

    ``ix_jobs_user_state`` already covers the filtered form.
    """
    where = [Job.user_id == user.id]
    if state is not None:
        where.append(Job.state == state)

    total = await session.scalar(select(func.count(Job.id)).where(*where))

    result = await session.execute(
        select(Job)
        .where(*where)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    jobs = list(result.scalars().all())

    # One query for the whole page instead of one COUNT per row.
    positions = await _queue_positions(session, jobs)
    return JobListResponse(
        jobs=[
            await _status(session, j, positions.get(j.id), position_known=True)
            for j in jobs
        ],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.post("/{job_id}/cancel", response_model=JobStatusResponse)
async def cancel_job(
    job_id: str = Path(..., description="Job UUID"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> JobStatusResponse:
    """Request cancellation.

    A queued job is cancelled here and now. A running job normally only gets the
    flag set — the worker checks it between sliding-window patches and unwinds
    cleanly, which is why this returns ``running`` rather than ``cancelled``.

    The exception is a running job whose **lease has already expired**: there is no
    live worker to read the flag, so setting it and returning ``running`` would
    leave the caller polling a job that will never move until the reclaim loop
    notices. Cancel it outright instead — the user asked for it to stop, and it has
    already stopped.
    """
    job = await _load_owned_job(session, user, job_id, for_update=True)
    if job.state not in _CANCELLABLE:
        raise conflict("job_not_cancellable", f"Job is {job.state}.")

    job.cancel_requested = True
    abandoned = (
        job.state == "running"
        and job.lease_expires_at is not None
        and job.lease_expires_at < utcnow()
    )
    if job.state in ("awaiting_upload", "queued") or abandoned:
        if job.upload_id:
            await storage.abort_multipart(job.volume_key, job.upload_id)
            job.upload_id = None
        job.state = "cancelled"
        job.message = (
            "Cancelled after the worker stopped responding"
            if abandoned
            else "Cancelled before it started"
        )
        job.finished_at = utcnow()
        job.lease_expires_at = None
        job.deadline_at = None
        await storage.delete_prefix(storage.job_prefix(user.id, job.id))
    else:
        job.message = "Cancelling"
    # Commit before responding: the worker polls cancel_requested, and a caller
    # that immediately re-reads the job must see the new state.
    await session.commit()
    return await _status(session, job)


@router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str = Path(..., description="Job UUID"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a finished job and purge its objects. Running jobs must be cancelled first."""
    job = await _load_owned_job(session, user, job_id, for_update=True)
    if job.state == "running":
        raise conflict("job_running", "Cancel the job before deleting it.")
    if job.upload_id:
        await storage.abort_multipart(job.volume_key, job.upload_id)
    await storage.delete_prefix(storage.job_prefix(user.id, job.id))
    await session.delete(job)
    await session.commit()


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@router.get("/{job_id}/result", status_code=307)
async def get_result(
    job_id: str = Path(..., description="Job UUID"),
    format: str = Query("contours", pattern="^(contours|mask)$"),
    redirect: bool = Query(
        True,
        description=(
            "False returns {\"url\": ...} instead of a 307. For browsers: see below."
        ),
    ),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Redirect to a short-lived presigned URL for the result object.

    A redirect rather than a proxied stream: contour JSON for a whole abdomen runs
    to tens of megabytes and there is no reason to push it through the API pods (or
    Cloudflare's request pipeline twice).

    **The 307 is the default and must stay the default.** The approved 2.0.1.0
    plugin depends on it (``VoxTellApiClient.cs:383`` follows the redirect), and
    changing it would need a re-approval on every workstation.

    ``?redirect=false`` exists for the browser, and it fixes a latent
    cross-origin failure rather than a stylistic one. A ``fetch`` that carries an
    ``Authorization`` header and then follows a 307 to ``s3.dicomsegvr.com``
    becomes a CORS request against SeaweedFS, whose ``-s3.allowedOrigins`` is
    ``https://dashboard.dicomsegvr.com`` **only** — so the redirect hop fails
    preflight from this hostname. Handing back the URL and letting the page
    navigate to it sidesteps CORS entirely, with no SeaweedFS restart.
    """
    job = await _load_owned_job(session, user, job_id)
    if job.state != "done":
        raise conflict("job_not_done", f"Job is {job.state}.")

    if format == "mask":
        if not job.mask_key:
            raise not_found("This job was not run with want_mask")
        key, filename = job.mask_key, f"{job.id}-mask.bin.gz"
    else:
        if not job.result_key:
            raise not_found("Result object is missing (it may have been purged)")
        key, filename = job.result_key, f"{job.id}-result.json.gz"

    url = await storage.presign_get(key, filename)
    if not redirect:
        return JSONResponse({"url": url, "filename": filename})
    return RedirectResponse(url=url, status_code=307)

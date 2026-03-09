"""
VoxTell ESAPI Bridge — FastAPI application.

Start with:
    VOXTELL_MODEL_DIR=/path/to/model uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1

IMPORTANT: Always use --workers 1.  The in-memory session/job store and the
single model instance are not safe to share across OS processes.
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .contour_utils import extract_contours
from .nifti_builder import build_affine_lps, build_nifti_from_array, decode_slice
from .schemas import (
    HealthResponse,
    InferenceAcceptedResponse,
    InferenceRequest,
    InferenceStatusResponse,
    SessionCreateRequest,
    SessionCreatedResponse,
    SliceUploadRequest,
    SliceUploadResponse,
)
from .session_manager import (
    JobData,
    SessionData,
    cleanup_loop,
    delete_session,
    get_job,
    get_session,
    jobs,
    sessions,
)
from .voxtell_worker import worker

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: model init + background cleanup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.session_dir).mkdir(parents=True, exist_ok=True)
    worker.initialize(settings.model_dir, settings.device_str, settings.text_model)
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="VoxTell ESAPI Bridge",
    description=(
        "Backend that converts Varian Eclipse ESAPI raw CT volumes into "
        "NIfTI, runs VoxTell AI segmentation, and returns DICOM-compatible "
        "LPS contour points for RT structure creation."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health():
    return HealthResponse(status="ok", model_loaded=worker.model_loaded)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@app.post(
    "/sessions",
    response_model=SessionCreatedResponse,
    status_code=201,
    tags=["sessions"],
)
def create_session(body: SessionCreateRequest):
    """
    Create a new session with volume metadata.

    Pre-allocates a numpy buffer for slice-by-slice upload.
    Upload slices via PUT /sessions/{id}/slices/{z}, then call
    POST /sessions/{id}/finalize to build the NIfTI.
    """
    session_id = str(uuid.uuid4())
    session_dir = Path(settings.session_dir) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    nifti_path = str(session_dir / "volume.nii.gz")

    affine_lps = build_affine_lps(
        row_direction=body.row_direction,
        col_direction=body.col_direction,
        slice_direction=body.slice_direction,
        x_res=body.x_res,
        y_res=body.y_res,
        z_res=body.z_res,
        origin=body.origin,
    )

    volume_buffer = np.zeros((body.z_size, body.y_size, body.x_size), dtype=np.int32)

    sessions[session_id] = SessionData(
        session_id=session_id,
        nifti_path=nifti_path,
        affine_lps=affine_lps.tolist(),
        original_shape=(body.x_size, body.y_size, body.z_size),
        volume_buffer=volume_buffer,
    )

    log.info(
        "Session %s created: shape=(%d,%d,%d) buffer=%d MB",
        session_id,
        body.x_size,
        body.y_size,
        body.z_size,
        volume_buffer.nbytes // (1024 * 1024),
    )

    return SessionCreatedResponse(session_id=session_id, slices_total=body.z_size)


@app.put(
    "/sessions/{session_id}/slices/{z_index}",
    response_model=SliceUploadResponse,
    tags=["sessions"],
)
def upload_slice(session_id: str, z_index: int, body: SliceUploadRequest):
    """Upload a single 2D slice into the pre-allocated volume buffer."""
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.finalized:
        raise HTTPException(status_code=409, detail="Session already finalized")

    x_size, y_size, z_size = session.original_shape
    if z_index < 0 or z_index >= z_size:
        raise HTTPException(
            status_code=422,
            detail=f"z_index {z_index} out of range [0, {z_size})",
        )

    try:
        slice_data = decode_slice(body.voxel_data_b64, x_size, y_size)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Slice decode failed: {exc}"
        ) from exc

    session.volume_buffer[z_index] = slice_data
    session.slices_received.add(z_index)

    return SliceUploadResponse(
        z_index=z_index,
        slices_received=len(session.slices_received),
        slices_total=z_size,
    )


@app.post(
    "/sessions/{session_id}/finalize",
    response_model=SessionCreatedResponse,
    tags=["sessions"],
)
def finalize_session(session_id: str):
    """
    Build the NIfTI file from the accumulated slice buffer.

    All slices must have been uploaded. After finalization the buffer is freed
    and inference jobs can be submitted against this session.
    """
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.finalized:
        raise HTTPException(status_code=409, detail="Session already finalized")

    _, _, z_size = session.original_shape
    missing = set(range(z_size)) - session.slices_received
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing {len(missing)} slice(s): {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}",
        )

    try:
        build_nifti_from_array(
            volume=session.volume_buffer,
            affine_lps=np.array(session.affine_lps),
            output_path=session.nifti_path,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"NIfTI build failed: {exc}"
        ) from exc

    session.volume_buffer = None
    session.finalized = True
    log.info("Session %s finalized (%d slices)", session_id, z_size)

    return SessionCreatedResponse(session_id=session_id, slices_total=z_size)


@app.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
def remove_session(session_id: str):
    """Delete a session and its associated NIfTI file immediately."""
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


async def _run_inference_job(job_id: str) -> None:
    """Background coroutine that drives a single inference job."""
    job = jobs[job_id]
    session = get_session(job.session_id)
    if session is None:
        job.status = "failed"
        job.error = f"Session {job.session_id} not found (may have expired)"
        return

    job.status = "running"
    log.info("Job %s running: prompts=%s", job_id, job.prompts)

    try:
        masks, props = await worker.predict(session.nifti_path, job.prompts)

        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None,
            extract_contours,
            masks,
            props,
            np.array(session.affine_lps),
            session.original_shape,
            job.prompts,
            False,  # include_mask
        )

        job.results = [r.model_dump() for r in results]
        job.status = "completed"
        log.info("Job %s completed", job_id)

    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        log.error("Job %s failed: %s", job_id, exc)


@app.post(
    "/inference",
    response_model=InferenceAcceptedResponse,
    status_code=202,
    tags=["inference"],
)
async def submit_inference(body: InferenceRequest, background_tasks: BackgroundTasks):
    """
    Submit a segmentation job.

    The session must be finalized before submitting inference.
    Poll `GET /inference/{job_id}` every 10 seconds until `status` is
    `"completed"` or `"failed"`.
    """
    session = get_session(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.finalized:
        raise HTTPException(
            status_code=409,
            detail="Session not finalized — call POST /sessions/{id}/finalize first",
        )

    job_id = str(uuid.uuid4())
    jobs[job_id] = JobData(
        job_id=job_id,
        session_id=body.session_id,
        prompts=body.prompts,
        status="pending",
    )

    background_tasks.add_task(_run_inference_job, job_id)
    log.info("Job %s queued for session %s", job_id, body.session_id)

    return InferenceAcceptedResponse(job_id=job_id)


@app.get(
    "/inference/{job_id}",
    response_model=InferenceStatusResponse,
    tags=["inference"],
)
def get_inference_status(job_id: str):
    """
    Poll inference job status.

    When `status == "completed"`, the `results` field contains one
    `SegmentationResult` per prompt, each with LPS contour points ready for
    `structure.AddContourOnImagePlane(contour, z_index)` in Varian ESAPI.
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return InferenceStatusResponse(
        job_id=job.job_id,
        status=job.status,
        error=job.error,
        results=job.results,
    )

"""VoxTell-Cloud API — FastAPI application factory and lifespan.

Everything is mounted under ``/v1`` because the Ingress path-splits one
hostname: ``/v1`` reaches this service, ``/`` reaches the console SPA.

Unlike the v1 single-user bridge this process is **stateless**: no sessions, no
model, no volume buffers. All state lives in Postgres and SeaweedFS, so it scales
horizontally and survives a restart mid-job.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import API_VERSION
from .auth import init_jwks
from .config import settings
from . import metrics as metrics_module
from .db import init_db
from .reclaim import reclaim_loop
from .routes import catalog, health, jobs, keys, me, metrics, qa, usage, volumes
from .sweeper import retention_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("voxtell.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_jwks()
    # Two loops on deliberately different cadences. Retention is slow, touches
    # object storage, and 15 minutes of lag costs nothing. Reclaim is fast, touches
    # only `jobs` through an index, and its latency is directly visible to a user
    # staring at a job frozen at 20 %. Both are guarded by their own advisory lock,
    # so running them on every replica is safe.
    background = [
        asyncio.create_task(retention_loop(), name="retention"),
        asyncio.create_task(reclaim_loop(), name="reclaim"),
        asyncio.create_task(metrics_module.refresh_loop(), name="metrics"),
    ]
    log.info("VoxTell-Cloud API %s ready", API_VERSION)
    yield
    for task in background:
        task.cancel()
    for task in background:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="VoxTell-Cloud API",
    version=API_VERSION,
    description=(
        "Multi-user backend for free-text-prompted 3D segmentation from Varian "
        "Eclipse. Upload a CT volume once, queue a job, poll it, download DICOM "
        "LPS contours ready for AddContourOnImagePlane."
    ),
    lifespan=lifespan,
    docs_url="/v1/docs",
    openapi_url="/v1/openapi.json",
    redoc_url=None,
)

# The console SPA is the only browser client; the ESAPI plugin is not subject to
# CORS. Credentials stay off — auth is a bearer header, never a cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def record_request_metrics(request, call_next):
    """Count and time every request, labelled by route **template**.

    Twenty lines instead of `prometheus-fastapi-instrumentator`, for one reason: the
    label must be the template (``/v1/jobs/{job_id}``) and never the concrete path.
    Most instrumentation libraries default to the path, which here would mint a new
    time series per job UUID — unbounded cardinality that eventually takes Prometheus
    down. Writing it out makes that choice explicit and reviewable.

    ``request.scope["route"]`` is only populated once routing has matched, so a 404
    has none; those collapse to a single ``unmatched`` label rather than echoing
    whatever an unauthenticated scanner asked for.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        status = 500
        raise
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "unmatched"
        elapsed = time.perf_counter() - started
        metrics_module.HTTP_REQUESTS.labels(
            method=request.method, route=template, status=str(status)
        ).inc()
        metrics_module.HTTP_LATENCY.labels(method=request.method, route=template).observe(elapsed)

# The volume routes are always mounted; each one 404s when
# VOXTELL_VOLUMES_ENABLED is off. Mounting conditionally would make the flag a
# restart-only setting and leave the OpenAPI document disagreeing with itself.
for router in (
    health.router,
    metrics.router,
    catalog.router,
    me.router,
    usage.router,
    keys.router,
    volumes.router,
    jobs.router,
    qa.router,
):
    app.include_router(router, prefix="/v1")

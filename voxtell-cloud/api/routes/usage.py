"""Usage over time, and shared-service state.

Two endpoints the dashboard cannot be built without.

``GET /v1/usage`` is the important one. ``usage_events`` has been appended to since
the first job ran and nothing has ever read it except a ``count(*)`` since the
start of the month — so the console could say "12 of 200 used" and nothing else.
No trend, no idea whether that 12 arrived today or over three weeks.

``GET /v1/system`` explains a wait. On a single-GPU service the honest answer to
"why is my job queued" is the global backlog, so it is served here rather than
inferred client-side from a job list that only shows the caller's own work.

BOTH ARE JWT-GATED, NOT KEY-GATED. ``get_console_user`` rather than
``get_caller``: a leaked ``vxt_`` workstation key should segment images, which is
what it is for, and not enumerate an account's billing-relevant history. The
approved plugin never calls either endpoint.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_console_user
from ..db import get_session
from ..estimate import wait_estimate_seconds
from ..metrics import SNAPSHOT
from ..models import User
from ..schemas import SystemResponse, UsageDay, UsageResponse

router = APIRouter(tags=["account"])

# Cap the window. Unbounded, this is a full-table scan per request on a table that
# only grows; 366 covers a year-over-year view, which is the most anyone has asked
# a segmentation dashboard for.
_MAX_DAYS = 366

# Bucketed in SQL rather than Python: Postgres groups 30 days of rows in one pass,
# and doing it here would mean shipping every event to the API pod to re-derive a
# date. `date_trunc('day', created_at)` at UTC matches quota.month_start(), so the
# daily columns and the monthly quota on the same screen agree.
#
# The index ix_usage_user_created (user_id, created_at) covers the WHERE and the
# GROUP BY key's leading column.
_BUCKETS_SQL = text(
    """
    SELECT date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS day,
           count(*)                        AS jobs,
           coalesce(sum(prompts), 0)       AS prompts,
           coalesce(sum(gpu_seconds), 0)   AS gpu_seconds,
           coalesce(sum(voxels), 0)        AS voxels
      FROM usage_events
     WHERE user_id = :uid
       AND created_at >= :since
     GROUP BY 1
     ORDER BY 1
    """
)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


@router.get("/usage", response_model=UsageResponse)
async def usage(
    days: int = Query(30, ge=1, le=_MAX_DAYS, description="Window length in days, including today"),
    user: User = Depends(get_console_user),
    session: AsyncSession = Depends(get_session),
) -> UsageResponse:
    """Daily usage for the caller, oldest first, zero-filled.

    Zero-filled server-side. A sparse response would make every client rebuild a
    calendar to plot it, and the first client to get that wrong draws a chart whose
    columns are evenly spaced but not evenly *timed* — which reads as a smooth
    workload where there were actually three idle days.
    """
    today = _utc_today()
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)

    rows = (await session.execute(_BUCKETS_SQL, {"uid": user.id, "since": since})).mappings().all()
    got = {r["day"]: r for r in rows}

    buckets: list[UsageDay] = []
    for i in range(days):
        d = first + timedelta(days=i)
        r = got.get(d)
        buckets.append(
            UsageDay(
                day=d.isoformat(),
                jobs=int(r["jobs"]) if r else 0,
                prompts=int(r["prompts"]) if r else 0,
                gpu_seconds=round(float(r["gpu_seconds"]), 2) if r else 0.0,
                voxels=int(r["voxels"]) if r else 0,
            )
        )

    return UsageResponse(
        days=buckets,
        window_days=days,
        since=first.isoformat(),
        total_jobs=sum(b.jobs for b in buckets),
        total_prompts=sum(b.prompts for b in buckets),
        total_gpu_seconds=round(sum(b.gpu_seconds for b in buckets), 2),
    )


@router.get("/system", response_model=SystemResponse)
async def system(
    user: User = Depends(get_console_user),
    session: AsyncSession = Depends(get_session),
) -> SystemResponse:
    """Queue depth, running count and worker liveness.

    Read from the cached Prometheus snapshot, so polling this is free. The wait
    estimate is the one live-ish number, and it is memoised for
    ``VOXTELL_SERVICE_RATE_TTL_SECONDS`` in ``estimate.py``.
    """
    view = SNAPSHOT.public_view()
    depth = int(view["queue_depth"])
    return SystemResponse(
        queue_depth=depth,
        running=int(view["running"]),
        worker_online=bool(view["worker_online"]),
        # What a job joining the back of the queue right now would wait.
        estimated_wait_seconds=await wait_estimate_seconds(session, depth),
        snapshot_age_seconds=float(view["snapshot_age_seconds"]),
    )

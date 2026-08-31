"""Caller identity and current usage."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_caller
from ..config import settings
from ..db import get_session
from ..lineage import lineage_secret_for
from ..models import User
from ..quota import load_state
from ..schemas import MeResponse

router = APIRouter(tags=["account"])


def _capabilities() -> list[str]:
    """Optional features this deployment has switched on.

    The plugin probes this instead of sniffing for a 404 on ``POST /v1/volumes``.
    That matters because the failure it replaces is asymmetric: a new DLL against
    an older API would otherwise discover the missing endpoint only *after* the
    operator clicked Upload, and the natural fallback — retry the legacy path —
    looks identical to a transient failure. A capability list makes the decision
    before any patient data moves.

    Newtonsoft ignores unknown JSON fields, so adding this is invisible to
    already-approved plugin builds.
    """
    caps: list[str] = []
    if settings.VOXTELL_VOLUMES_ENABLED:
        caps.append("volumes")
    return caps


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    """Works with either credential kind, so the plugin can verify a key."""
    state = await load_state(session, user)
    volumes_on = settings.VOXTELL_VOLUMES_ENABLED
    return MeResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        monthly_quota=state.limit,
        used_this_month=state.used,
        outstanding=state.outstanding,
        max_outstanding=state.max_outstanding,
        capabilities=_capabilities(),
        volume_ttl_minutes=settings.VOXTELL_VOLUME_TTL_MINUTES if volumes_on else None,
        # Additive. `QuotaState` has carried all three for a while and `load_state`
        # already computes them; they were being collapsed into `outstanding` before
        # reaching the wire, so the console could say "1/2 in flight" but never
        # whether the job was waiting for the GPU or on it.
        queued=state.queued,
        running=state.running,
        remaining=state.remaining,
        # None when this deployment has not provisioned a lineage secret, which
        # the plugin reads as "QA lineage is off" and stops sending keys.
        lineage_secret=lineage_secret_for(user.id),
    )

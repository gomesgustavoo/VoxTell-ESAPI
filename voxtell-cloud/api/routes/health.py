"""Liveness / readiness and the OIDC discovery document for the ESAPI plugin."""

from __future__ import annotations

from fastapi import APIRouter

from .. import API_VERSION
from ..config import settings
from ..db import ping
from ..schemas import AuthConfigResponse, HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Always 200 while the process is alive; ``database`` reports reachability.

    Kept non-failing on a DB blip so a transient Postgres restart does not make
    Kubernetes kill every API replica at once.
    """
    db_ok = await ping()
    return HealthResponse(
        status="ok" if db_ok else "degraded", database=db_ok, version=API_VERSION
    )


@router.get("/auth/config", response_model=AuthConfigResponse)
def auth_config() -> AuthConfigResponse:
    """Endpoints for both OAuth2 grants the ESAPI plugin can use.

    The plugin fetches this once (unauthenticated) so realm URLs are not baked
    into a compiled DLL that ships to hospital workstations. It prefers
    Authorization Code + PKCE with a loopback redirect and falls back to the
    device code flow, which is why both sets of endpoints are advertised
    together. ``pkce_method`` is required by both grants — see
    :class:`AuthConfigResponse`.
    """
    base = settings.OIDC_ISSUER.rstrip("/")
    oidc = f"{base}/protocol/openid-connect"
    return AuthConfigResponse(
        issuer=base,
        device_client_id=settings.OIDC_DEVICE_CLIENT_ID,
        device_authorization_endpoint=f"{oidc}/auth/device",
        token_endpoint=f"{oidc}/token",
        audience=settings.OIDC_AUDIENCE,
        authorization_endpoint=f"{oidc}/auth",
        scopes=settings.OIDC_PLUGIN_SCOPES,
        redirect_ports=list(settings.OIDC_REDIRECT_PORTS),
    )

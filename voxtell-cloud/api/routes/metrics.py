"""The ``/v1/metrics`` scrape endpoint.

Mounted under ``/v1`` deliberately: ``85-voxtell-ingress.yaml`` path-splits one
hostname (``/v1`` -> api, ``/`` -> console), so anything under ``/v1`` is routed with
no Ingress change and — more importantly — no new public hostname, which on this
cluster costs manual Cloudflare tunnel work.

The consequence is that it is reachable from the internet, so it is **gated**. Queue
depth, tenant counts and monthly usage are commercially sensitive and would also let
an unauthenticated observer watch clinical activity levels. Prometheus sends the token
as a bearer credential in its scrape config.

Comparison is constant-time. This is a static shared secret compared on every scrape,
which is exactly the shape that leaks to a timing attack.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response

from .. import metrics
from ..config import settings
from ..errors import not_found, unauthorized

router = APIRouter(tags=["system"])

# The content type Prometheus expects; anything else is parsed as a failure.
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", include_in_schema=False)
async def scrape(request: Request) -> Response:
    """Prometheus exposition. 404 when disabled, 401 without the token.

    404 rather than 403 when the feature is off, so an operator cannot distinguish
    "this deployment has metrics but you lack the token" from "this deployment has no
    metrics" — the same reasoning ``_load_owned_job`` uses for unparseable job ids.
    """
    if not settings.VOXTELL_METRICS_ENABLED:
        raise not_found("Not found")

    expected = settings.VOXTELL_METRICS_TOKEN
    if not expected:
        # Enabled but unconfigured. Refusing is the safe direction: silently serving
        # it unauthenticated because a Secret key was missing is how this leaks.
        raise unauthorized("Metrics token is not configured")

    presented = request.headers.get("x-metrics-token") or ""
    if not presented:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            presented = auth[7:]

    if not hmac.compare_digest(presented, expected):
        raise unauthorized("Invalid metrics token")

    return Response(content=metrics.render(), media_type=_CONTENT_TYPE)

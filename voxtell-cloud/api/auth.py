"""Dual bearer authentication: Keycloak JWTs and VoxTell API keys.

Both resolve to the same ``users`` row, so a job started from the console and a
job started from Eclipse belong to one identity.

* **Keycloak JWT** (console, and the ESAPI plugin's device-code flow) — RS256,
  validated against the realm JWKS with ``aud``/``iss`` pinned. On first-seen
  ``sub`` the user row is provisioned; the replicas can race on the same first
  request, so a unique-violation is caught and the row re-read.
* **API key** (``vxt_...``) — opaque, 256 bits of ``token_urlsafe``. Only the
  SHA-256 hex is stored, so revocation is immediate and a DB leak yields nothing
  usable. A JWT can never be mistaken for one: JWTs are base64url header
  segments and cannot begin with ``vxt_``.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import timedelta

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .errors import unauthorized
from .models import ApiKey, User, utcnow

# auto_error=False so we emit our own 401 with WWW-Authenticate.
_bearer = HTTPBearer(auto_error=False)

API_KEY_PREFIX = "vxt_"
# How much of the plaintext the console may display to identify a key.
API_KEY_LABEL_CHARS = 12
# Throttle last_used_at writes: one row update per key per interval, not per call.
_LAST_USED_INTERVAL = timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
def generate_api_key() -> tuple[str, str, str]:
    """Mint a key. Returns ``(plaintext, prefix, sha256_hex)``.

    The plaintext is returned to the caller exactly once and never stored.
    """
    token = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return token, token[:API_KEY_LABEL_CHARS], hash_api_key(token)


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------- #
# JWKS
# --------------------------------------------------------------------------- #
class JwksCache:
    """Signing keys by kid, with a single-flight refetch on an unknown kid.

    Keycloak rotates keys; rather than polling we refetch lazily the first time a
    token presents a kid we do not know, holding a lock so the replicas do not
    stampede the realm endpoint.
    """

    def __init__(self, jwks_url: str) -> None:
        self._jwks_url = jwks_url
        self._keys: dict[str, object] = {}
        self._lock = asyncio.Lock()

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(self._jwks_url)
            resp.raise_for_status()
            data = resp.json()
        keys: dict[str, object] = {}
        for jwk in data.get("keys", []):
            kid = jwk.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = RSAAlgorithm.from_jwk(jwk)
            except Exception:
                # Skip non-RSA / malformed entries instead of failing the refresh.
                continue
        self._keys = keys

    async def refresh(self) -> None:
        async with self._lock:
            await self._fetch()

    async def get_key(self, kid: str) -> object:
        key = self._keys.get(kid)
        if key is not None:
            return key
        async with self._lock:
            # Another coroutine may have populated it while we waited.
            key = self._keys.get(kid)
            if key is None:
                await self._fetch()
                key = self._keys.get(kid)
        if key is None:
            raise unauthorized("Unknown signing key")
        return key


jwks_cache = JwksCache(settings.OIDC_JWKS_URL)


async def init_jwks() -> None:
    """Warm the cache at startup; a transient failure is fine (lazy refetch)."""
    try:
        await jwks_cache.refresh()
    except Exception:
        pass


async def decode_token(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise unauthorized("Malformed token") from exc

    kid = header.get("kid")
    if not kid:
        raise unauthorized("Missing kid")

    key = await jwks_cache.get_key(kid)
    try:
        return jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=settings.OIDC_AUDIENCE,
            issuer=settings.OIDC_ISSUER,
            leeway=settings.JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise unauthorized("Token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise unauthorized("Invalid audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise unauthorized("Invalid issuer") from exc
    except jwt.InvalidTokenError as exc:
        raise unauthorized("Invalid token") from exc


# --------------------------------------------------------------------------- #
# User resolution
# --------------------------------------------------------------------------- #
async def _get_or_create_user(session: AsyncSession, claims: dict) -> User:
    sub = claims.get("sub")
    if not sub:
        raise unauthorized("Missing subject")

    result = await session.execute(select(User).where(User.keycloak_sub == sub))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    user = User(
        keycloak_sub=sub,
        email=claims.get("email"),
        username=claims.get("preferred_username") or claims.get("email") or sub,
        monthly_job_quota=settings.VOXTELL_DEFAULT_MONTHLY_QUOTA,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Lost the race with another replica — re-read the winner's row.
        await session.rollback()
        result = await session.execute(select(User).where(User.keycloak_sub == sub))
        user = result.scalar_one_or_none()
        if user is None:
            raise unauthorized("Could not provision user")
    return user


async def _user_from_api_key(session: AsyncSession, token: str) -> tuple[User, ApiKey]:
    result = await session.execute(
        select(ApiKey).where(ApiKey.token_hash == hash_api_key(token))
    )
    key = result.scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        raise unauthorized("API key invalid or revoked")

    now = utcnow()
    if key.expires_at is not None and key.expires_at <= now:
        raise unauthorized("API key expired")

    if key.last_used_at is None or (now - key.last_used_at) > _LAST_USED_INTERVAL:
        key.last_used_at = now

    user = await session.get(User, key.user_id)
    if user is None:
        raise unauthorized("API key owner no longer exists")
    return user, key


async def get_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the caller from either credential kind. The default dependency."""
    if credentials is None or not credentials.credentials:
        raise unauthorized("Missing bearer token")
    if credentials.scheme.lower() != "bearer":
        raise unauthorized("Invalid auth scheme")

    token = credentials.credentials
    if token.startswith(API_KEY_PREFIX):
        user, key = await _user_from_api_key(session, token)
        request.state.api_key = key
    else:
        claims = await decode_token(token)
        user = await _get_or_create_user(session, claims)
        request.state.claims = claims

    if user.disabled_at is not None:
        raise unauthorized("Account disabled")
    return user


async def get_console_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Keycloak-JWT-only caller.

    Key management is deliberately closed to API keys: a leaked key must not be
    able to mint more keys or widen its own blast radius.
    """
    if credentials is None or not credentials.credentials:
        raise unauthorized("Missing bearer token")
    if credentials.scheme.lower() != "bearer":
        raise unauthorized("Invalid auth scheme")
    if credentials.credentials.startswith(API_KEY_PREFIX):
        raise unauthorized("This endpoint requires an interactive sign-in, not an API key")

    claims = await decode_token(credentials.credentials)
    user = await _get_or_create_user(session, claims)
    request.state.claims = claims
    if user.disabled_at is not None:
        raise unauthorized("Account disabled")
    return user

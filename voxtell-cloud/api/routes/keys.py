"""API key management. Keycloak-JWT only — see ``auth.get_console_user``."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import generate_api_key, get_console_user
from ..db import get_session
from ..errors import not_found
from ..models import ApiKey, User, utcnow
from ..schemas import ApiKeyCreatedResponse, ApiKeyCreateRequest, ApiKeyResponse
from datetime import timedelta

router = APIRouter(prefix="/keys", tags=["keys"])


@router.get("", response_model=list[ApiKeyResponse])
async def list_keys(
    user: User = Depends(get_console_user),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKey]:
    """All keys ever issued to the caller, revoked ones included (as an audit trail)."""
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("", response_model=ApiKeyCreatedResponse, status_code=201)
async def create_key(
    body: ApiKeyCreateRequest,
    user: User = Depends(get_console_user),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreatedResponse:
    """Mint a key. The plaintext is in this response and nowhere else, ever."""
    token, prefix, token_hash = generate_api_key()
    expires_at = (
        utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    )
    key = ApiKey(
        user_id=user.id,
        name=body.name.strip(),
        prefix=prefix,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(key)
    # Commit before responding: the plaintext is returned once and the caller may
    # authenticate with it immediately.
    await session.commit()
    return ApiKeyCreatedResponse(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        created_at=key.created_at,
        last_used_at=None,
        expires_at=key.expires_at,
        revoked_at=None,
        token=token,
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: str = Path(..., description="API key UUID"),
    user: User = Depends(get_console_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke immediately. The row is kept (audit) with ``revoked_at`` stamped."""
    try:
        kid = uuid.UUID(key_id)
    except (ValueError, AttributeError):
        raise not_found("API key not found")

    result = await session.execute(
        select(ApiKey).where(ApiKey.id == kid, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise not_found("API key not found")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        # Revocation must be effective the instant this returns 204.
        await session.commit()

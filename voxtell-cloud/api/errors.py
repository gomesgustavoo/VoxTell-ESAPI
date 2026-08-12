"""Shared HTTPException constructors so error shapes stay consistent.

Machine-readable errors use ``{"error": "<code>", ...}`` as the detail so the
C# client can branch on a stable string instead of parsing prose.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


def unauthorized(detail: str = "Unauthorized") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden(detail: str = "Forbidden") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def not_found(detail: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def bad_request(detail: Any = "Bad request") -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def conflict(error: str, message: str) -> HTTPException:
    """409 — the resource exists but is in the wrong state for this call."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": error, "message": message},
    )


def payload_too_large(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail={"error": error, "message": message},
    )


def too_many_requests(error: str, message: str, retry_after: int) -> HTTPException:
    """429 with Retry-After — the per-user concurrency cap, not a rate limit.

    The ESAPI client is expected to honour Retry-After and resubmit rather than
    treating this as fatal.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": error, "message": message, "retryAfter": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


def quota_exceeded(used: int, limit: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={"error": "monthly_quota_exceeded", "used": used, "limit": limit},
    )

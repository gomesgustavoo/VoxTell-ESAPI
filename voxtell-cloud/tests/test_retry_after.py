"""The wait estimator behind ``Retry-After`` and ``estimated_wait_seconds``.

``Retry-After`` was the constant 30. The ESAPI client *honours* it, so on a queue
twenty jobs deep — twenty minutes of real work — the client re-POSTed every thirty
seconds and collected a 429 each time. That is a self-inflicted retry storm against
Traefik's rate limit, and a planner who concludes the service is broken rather than
busy. The number now comes from measured throughput.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from api import estimate
from api.config import settings

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    estimate.reset_cache()


def _completed(sync_engine: Engine, user_id: uuid.UUID, *durations: float) -> None:
    """Record finished jobs with known GPU durations, for the median to find."""
    with sync_engine.begin() as conn:
        for seconds in durations:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, user_id, state, prompts, geometry, volume_key, "
                    "                  started_at, finished_at) "
                    "VALUES (:id, :uid, 'done', '[\"liver\"]'::jsonb, '{}'::jsonb, 'k', "
                    f"        now() - interval '{seconds} seconds', now())"
                ),
                {"id": uuid.uuid4(), "uid": user_id},
            )


async def test_no_history_falls_back_to_the_configured_default(
    db_session: AsyncSession,
) -> None:
    """A fresh deployment must produce a sane number, not a crash or a zero."""
    rate = await estimate.service_rate_seconds(db_session)
    assert rate == pytest.approx(settings.VOXTELL_DEFAULT_JOB_SECONDS)


async def test_rate_is_the_median_not_the_mean(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    """Durations here are strongly right-skewed — measured 0.4 s to 76 s.

    One large study would drag a mean far off, which is why this is a percentile.
    """
    uid = make_user()
    _completed(sync_engine, uid, 10, 10, 10, 10, 600)

    rate = await estimate.service_rate_seconds(db_session)

    assert rate == pytest.approx(10, abs=1.5), f"got {rate}; a mean would be ~128"


async def test_empty_queue_returns_the_floor(db_session: AsyncSession) -> None:
    """Never tell a client to come back sooner than its own poll cadence."""
    assert await estimate.wait_estimate_seconds(db_session, 0) == (
        settings.VOXTELL_POLL_INTERVAL_SECONDS
    )


async def test_estimate_grows_with_queue_depth(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    _completed(sync_engine, make_user(), *[20.0] * 5)

    one = await estimate.wait_estimate_seconds(db_session, 1)
    three = await estimate.wait_estimate_seconds(db_session, 3)

    assert one < three, "a deeper queue must not report a shorter wait"
    assert three == pytest.approx(60, abs=6)


async def test_estimate_is_capped(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    """Past ten minutes the honest advice is 'later', not a precise number.

    An uncapped value also produces a Retry-After long enough that the client looks
    hung to the user.
    """
    _completed(sync_engine, make_user(), *[60.0] * 5)

    assert await estimate.wait_estimate_seconds(db_session, 10_000) == (
        settings.VOXTELL_MAX_RETRY_AFTER_SECONDS
    )


async def test_estimate_is_never_below_the_floor(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    """Sub-second jobs are real — 0.4 s was measured — and must not yield 0."""
    _completed(sync_engine, make_user(), *[0.4] * 5)

    assert await estimate.wait_estimate_seconds(db_session, 1) >= (
        settings.VOXTELL_POLL_INTERVAL_SECONDS
    )


async def test_zero_measured_rate_does_not_collapse_the_estimate(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    """started_at == finished_at is possible; it must not make every wait 0."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, user_id, state, prompts, geometry, volume_key, "
                "                  started_at, finished_at) "
                "VALUES (:id, :uid, 'done', '[\"liver\"]'::jsonb, '{}'::jsonb, 'k', "
                "        now(), now())"
            ),
            {"id": uuid.uuid4(), "uid": make_user()},
        )

    assert await estimate.service_rate_seconds(db_session) > 0


async def test_rate_is_cached(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    """This feeds a retry hint, so a minute of staleness beats a scan per request."""
    uid = make_user()
    _completed(sync_engine, uid, *[10.0] * 5)
    first = await estimate.service_rate_seconds(db_session)

    _completed(sync_engine, uid, *[600.0] * 20)
    assert await estimate.service_rate_seconds(db_session) == first

    estimate.reset_cache()
    assert await estimate.service_rate_seconds(db_session) != first


async def test_unqueued_jobs_are_ignored(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """Only completed work informs throughput; in-flight jobs have no duration."""
    uid = make_user()
    for state in ("queued", "running", "failed", "cancelled"):
        make_job(uid, state=state)

    assert await estimate.service_rate_seconds(db_session) == pytest.approx(
        settings.VOXTELL_DEFAULT_JOB_SECONDS
    )

"""Admission control: the gates, and the lock that makes them mean anything.

``admit()`` is the single binding gate for every job that reaches the GPU, and it
had **no tests at all**. The one that matters most is
``test_last_slot_is_not_double_spent``: the ``SELECT ... FOR UPDATE`` on the user row
exists purely so two simultaneous submissions cannot both see room for the last
slot, and until now nothing verified it did.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Callable

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api import estimate, quota
from api.config import settings
from api.models import Job, UsageEvent, User

pytestmark = pytest.mark.pg


async def _user(session: AsyncSession, user_id: uuid.UUID) -> User:
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


def _bill(sync_engine: Engine, user_id: uuid.UUID, n: int) -> None:
    """Charge n jobs against this month's quota, as submit_job would."""
    with sync_engine.begin() as conn:
        for _ in range(n):
            conn.execute(
                text(
                    "INSERT INTO usage_events (id, user_id, job_id, prompts) "
                    "VALUES (:id, :uid, :jid, 1)"
                ),
                {"id": uuid.uuid4(), "uid": user_id, "jid": uuid.uuid4()},
            )


# ------------------------------------------------------------------- monthly quota


async def test_admits_below_the_monthly_limit(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    uid = make_user(monthly_job_quota=5)
    _bill(sync_engine, uid, 4)

    state = await quota.admit(db_session, await _user(db_session, uid))

    assert state.used == 4 and state.limit == 5
    assert state.remaining == 1


async def test_refuses_at_the_monthly_limit(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    uid = make_user(monthly_job_quota=5)
    _bill(sync_engine, uid, 5)

    with pytest.raises(HTTPException) as caught:
        await quota.admit(db_session, await _user(db_session, uid))

    assert caught.value.status_code == 402
    # The C# client pins this exact string (ApiModels.cs) — do not rename it.
    assert caught.value.detail["error"] == "monthly_quota_exceeded"
    assert caught.value.detail["used"] == 5 and caught.value.detail["limit"] == 5


async def test_null_quota_means_unlimited(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    """NULL is a deliberate operator action, not a missing value."""
    uid = make_user(monthly_job_quota=None)
    _bill(sync_engine, uid, 10_000)

    state = await quota.admit(db_session, await _user(db_session, uid))
    assert state.limit is None and state.remaining is None


async def test_last_months_usage_does_not_count(
    db_session: AsyncSession, sync_engine: Engine, make_user: Callable[..., uuid.UUID]
) -> None:
    uid = make_user(monthly_job_quota=2)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO usage_events (id, user_id, job_id, prompts, created_at) "
                "VALUES (:id, :uid, :jid, 1, date_trunc('month', now()) - interval '1 day')"
            ),
            {"id": uuid.uuid4(), "uid": uid, "jid": uuid.uuid4()},
        )

    state = await quota.admit(db_session, await _user(db_session, uid))
    assert state.used == 0, "quota is per calendar month (UTC), not a rolling window"


# --------------------------------------------------------------- outstanding cap


async def test_refuses_over_the_outstanding_cap(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    uid = make_user()
    cap = settings.VOXTELL_MAX_RUNNING_PER_USER + settings.VOXTELL_MAX_QUEUED_PER_USER
    for _ in range(cap):
        make_job(uid, state="queued")

    with pytest.raises(HTTPException) as caught:
        await quota.admit(db_session, await _user(db_session, uid))

    assert caught.value.status_code == 429
    assert caught.value.detail["error"] == "too_many_outstanding_jobs"
    assert "Retry-After" in caught.value.headers


async def test_awaiting_upload_does_not_consume_a_slot(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """The lockout this whole split exists to prevent.

    ``awaiting_upload`` was once in OUTSTANDING_STATES, so six failed uploads inside
    the 120-minute reap window denied a user the GPU entirely — while advertising
    ``Retry-After: 30``. A job waiting for bytes is not competing for the GPU.
    """
    uid = make_user()
    for _ in range(20):
        make_job(uid, state="awaiting_upload", queued_at=None)

    state = await quota.admit(db_session, await _user(db_session, uid))
    assert state.outstanding == 0


async def test_terminal_jobs_do_not_consume_a_slot(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    uid = make_user()
    for state_name in ("done", "failed", "cancelled", "expired"):
        for _ in range(5):
            make_job(uid, state=state_name)

    assert (await quota.admit(db_session, await _user(db_session, uid))).outstanding == 0


async def test_queued_and_running_are_counted_separately(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """``queued`` alone is what fair_rank is assigned from, so it must be exact."""
    uid = make_user()
    make_job(uid, state="running")
    make_job(uid, state="queued")
    make_job(uid, state="queued")

    state = await quota.load_state(db_session, await _user(db_session, uid))
    assert (state.queued, state.running, state.outstanding) == (2, 1, 3)


# ------------------------------------------------------------------- global cap


async def test_refuses_when_the_global_queue_is_full(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """A user inside their own allowance still gets turned away — but honestly."""
    # Spread across users so nobody trips the per-user cap first.
    for _ in range(settings.VOXTELL_MAX_GLOBAL_QUEUED):
        make_job(make_user(), state="queued")
    newcomer = make_user()

    with pytest.raises(HTTPException) as caught:
        await quota.admit(db_session, await _user(db_session, newcomer))

    assert caught.value.status_code == 429
    assert caught.value.detail["error"] == "queue_full", (
        "a busy service must not blame the caller for having too many jobs"
    )


async def test_per_user_cap_is_reported_before_the_global_one(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """Ordering matters for the message the user reads."""
    uid = make_user()
    for _ in range(settings.VOXTELL_MAX_GLOBAL_QUEUED):
        make_job(make_user(), state="queued")
    cap = settings.VOXTELL_MAX_RUNNING_PER_USER + settings.VOXTELL_MAX_QUEUED_PER_USER
    for _ in range(cap):
        make_job(uid, state="queued")

    with pytest.raises(HTTPException) as caught:
        await quota.admit(db_session, await _user(db_session, uid))

    assert caught.value.detail["error"] == "too_many_outstanding_jobs"


# ------------------------------------------------------- the lock, and fair_rank


async def test_last_slot_is_not_double_spent(
    async_engine: AsyncEngine,
    schema_ready: None,
    sync_engine: Engine,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """Two concurrent submits at the last free slot: exactly one may win.

    This is the entire purpose of ``SELECT ... FOR UPDATE`` in ``admit()``. Separate
    sessions are essential — two coroutines on one session would serialise on the
    connection and prove nothing. Each admits and then *inserts*, as the real
    routes do, because the second caller must observe the first caller's row.
    """
    uid = make_user()
    cap = settings.VOXTELL_MAX_RUNNING_PER_USER + settings.VOXTELL_MAX_QUEUED_PER_USER
    for _ in range(cap - 1):
        make_job(uid, state="queued")

    maker = async_sessionmaker(bind=async_engine, expire_on_commit=False, autoflush=False)

    async def submit() -> str:
        async with maker() as session:
            try:
                state = await quota.admit(session, await _user(session, uid))
                session.add(
                    Job(
                        user_id=uid,
                        state="queued",
                        fair_rank=state.queued,
                        prompts=["liver"],
                        geometry={},
                        volume_key="k",
                    )
                )
                session.add(UsageEvent(user_id=uid, job_id=uuid.uuid4(), prompts=1))
                await session.commit()
                return "admitted"
            except HTTPException as exc:
                await session.rollback()
                return str(exc.detail.get("error", exc.status_code))

    results = await asyncio.gather(submit(), submit())

    assert sorted(results) == ["admitted", "too_many_outstanding_jobs"], (
        f"both submissions passed the cap: {results}"
    )
    with sync_engine.connect() as conn:
        total = conn.execute(
            text("SELECT count(*) FROM jobs WHERE user_id = :u AND state = 'queued'"),
            {"u": uid},
        ).scalar()
    assert total == cap, f"queue depth {total} exceeds the cap {cap}"


async def test_fair_rank_comes_from_the_users_own_queue_depth(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
) -> None:
    """Successive submissions get 0, 1, 2 — the shape round-robin depends on.

    The job is inserted through the **same session** and committed each round, as
    ``submit_job`` does. Using the sync ``make_job`` fixture here would deadlock:
    ``admit()`` still holds ``FOR UPDATE`` on the user row, and an INSERT into
    ``jobs`` from another connection needs ``FOR KEY SHARE`` on that same row via
    the foreign key.
    """
    uid = make_user()
    for expected in range(3):
        state = await quota.admit(db_session, await _user(db_session, uid))
        assert state.queued == expected, (
            "fair_rank must be this user's waiting count, not a global one"
        )
        db_session.add(
            Job(
                user_id=uid,
                state="queued",
                fair_rank=state.queued,
                prompts=["liver"],
                geometry={},
                volume_key="k",
            )
        )
        await db_session.commit()


async def test_fair_rank_ignores_other_users(
    db_session: AsyncSession,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """If it counted globally, round-robin would collapse back into FIFO."""
    mine, theirs = make_user(), make_user()
    for _ in range(4):
        make_job(theirs, state="queued")

    state = await quota.admit(db_session, await _user(db_session, mine))
    assert state.queued == 0


@pytest.fixture(autouse=True)
def _fresh_rate_cache() -> None:
    """The estimator memoises; a stale rate would leak across these tests."""
    estimate.reset_cache()

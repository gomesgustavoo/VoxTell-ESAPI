"""``queue_position`` and its ordering key.

Two properties, and the second is the interesting one.

The number must agree with the **dispatch ordering key**
(``COALESCE(queued_at, created_at)``), because a position derived from a different
column than the claim uses can only ever lie. That was the state of things: the
claim ordered by ``created_at``, so a job that spent twenty minutes uploading was
reported — correctly, and uselessly — as being near the front.

And it stays **global** rather than per-user. There is one GPU, so the global
backlog *is* the wait; a per-user count would say "you are next" while the job sits
behind a dozen others. What a shared-GPU service owes its users is an honest depth,
not a flattering one. ``test_position_is_global_by_design`` records that as a
decision so nobody "fixes" it later.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Job
from api.routes.jobs import _queue_position, _status

pytestmark = pytest.mark.pg


async def _job(session: AsyncSession, job_id: uuid.UUID) -> Job:
    session.expire_all()
    return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def test_position_is_zero_for_the_head_of_the_queue(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    head = make_job(make_user(), queued_at="now() - interval '10 minutes'")
    make_job(make_user(), queued_at="now() - interval '1 minute'")

    assert await _queue_position(db_session, await _job(db_session, head)) == 0


async def test_position_counts_only_queued_jobs(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """A running or finished job is not ahead of you in any useful sense."""
    for state in ("running", "done", "failed", "cancelled", "expired"):
        make_job(make_user(), state=state, queued_at="now() - interval '1 hour'")
    mine = make_job(make_user(), queued_at="now()")

    assert await _queue_position(db_session, await _job(db_session, mine)) == 0


async def test_position_is_null_unless_queued(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    for state in ("awaiting_upload", "running", "done", "failed", "cancelled", "expired"):
        jid = make_job(make_user(), state=state, queued_at=None)
        assert await _queue_position(db_session, await _job(db_session, jid)) is None


async def test_ordering_key_matches_the_claim_not_created_at(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """The specific lie: a slow upload reported as near the front.

    ``late`` was reserved 20 minutes ago but only became runnable seconds ago, so it
    is *behind* ``ready``, which has been runnable for five minutes. Ordering by
    ``created_at`` would report the opposite.
    """
    late = make_job(
        make_user(),
        created_at="now() - interval '20 minutes'",
        queued_at="now() - interval '10 seconds'",
    )
    ready = make_job(
        make_user(),
        created_at="now() - interval '5 minutes'",
        queued_at="now() - interval '5 minutes'",
    )

    assert await _queue_position(db_session, await _job(db_session, ready)) == 0
    assert await _queue_position(db_session, await _job(db_session, late)) == 1


async def test_null_queued_at_falls_back_to_created_at(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """Historic rows and any future path may leave queued_at NULL.

    A bare ``queued_at`` comparison would drop them out of the count entirely, and
    in the claim's ``ORDER BY`` a NULL sorts *last* in ASC — silently sending a job
    to the back of the queue forever. COALESCE is not decoration.
    """
    legacy = make_job(make_user(), created_at="now() - interval '30 minutes'", queued_at=None)
    newer = make_job(make_user(), queued_at="now()")

    assert await _queue_position(db_session, await _job(db_session, legacy)) == 0
    assert await _queue_position(db_session, await _job(db_session, newer)) == 1


async def test_position_is_global_by_design(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """Deliberate: the shared backlog is the real wait on a single GPU.

    If this ever becomes per-user, that is a product decision about disclosure —
    make it on purpose, and change this test with it.
    """
    others = make_user()
    for _ in range(4):
        make_job(others, queued_at="now() - interval '10 minutes'")
    mine = make_job(make_user(), queued_at="now()")

    assert await _queue_position(db_session, await _job(db_session, mine)) == 4


async def test_status_reports_an_eta_alongside_the_position(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """``estimated_wait_seconds`` is the field clients should actually show."""
    from api import estimate

    estimate.reset_cache()
    for _ in range(3):
        make_job(make_user(), queued_at="now() - interval '10 minutes'")
    mine = make_job(make_user(), queued_at="now()")

    status = await _status(db_session, await _job(db_session, mine))

    assert status.queue_position == 3
    assert status.estimated_wait_seconds is not None
    assert status.estimated_wait_seconds > 0


async def test_status_omits_the_eta_when_not_queued(
    db_session: AsyncSession, make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    jid = make_job(make_user(), state="running")

    status = await _status(db_session, await _job(db_session, jid))

    assert status.queue_position is None
    assert status.estimated_wait_seconds is None

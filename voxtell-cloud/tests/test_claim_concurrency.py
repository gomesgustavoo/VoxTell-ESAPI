"""The dispatch contract: locking, and the fairness it does not yet have.

Written against the **current** ``_CLAIM_SQL`` on purpose, before the Phase 4
rewrite, so the queue changes have a real before/after rather than a hope.

Two kinds of test live here:

* Invariants that must hold forever — no double-claim, ``SKIP LOCKED`` rather than
  ``FOR UPDATE``/``NOWAIT``, one ``attempts`` increment per claim. These are also
  the tests that fail loudly if anyone ever rewrites the claim with a window
  function, because **Postgres rejects ``FOR UPDATE`` in any query containing
  one** — which is exactly why the planned fair-share rank is a stored column.

* ``xfail(strict=True)`` tests describing the behaviour Phase 4 introduces. Strict
  means they **fail the suite once they start passing**, so they cannot be left
  behind as stale decoration: making fair-share work forces you to come here and
  delete the marker.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from worker import job as worker_job
from worker.settings import settings as worker_settings

pytestmark = pytest.mark.pg


def _claim_many(n_threads: int) -> list[worker_job.ClaimedJob | None]:
    """Fire n_threads simultaneous claims through a barrier, return every result."""
    barrier = threading.Barrier(n_threads)

    def one(i: int) -> worker_job.ClaimedJob | None:
        barrier.wait(timeout=10)
        return worker_job.claim_next(f"w{i}")

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        return list(pool.map(one, range(n_threads)))


def test_no_job_is_ever_claimed_twice(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """The core safety property. One job per claimant, no duplicates.

    Distinct users per job because the per-user running cap is 1 — with one user,
    only a single job would be claimable at all, which would make this vacuous.
    """
    expected = {make_job(make_user()) for _ in range(6)}

    claimed = [c for c in _claim_many(8) if c is not None]
    ids = [c.job_id for c in claimed]

    assert len(ids) == len(set(ids)), f"the same job was handed to two workers: {ids}"
    assert set(ids) == expected, "every queued job should have been dispatched exactly once"


def test_claim_skips_locked_rows_instead_of_blocking(
    worker_db: Engine,
    sync_engine: Engine,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
) -> None:
    """SKIP LOCKED, not FOR UPDATE and not NOWAIT — distinguished by one test.

    Hold a row lock on the oldest queued job, then claim. Correct behaviour is to
    return the *next* job promptly. Plain ``FOR UPDATE`` would block until the
    holder commits; ``NOWAIT`` would raise.
    """
    user_a, user_b = make_user(), make_user()
    locked = make_job(user_a, created_at="now() - interval '10 minutes'")
    available = make_job(user_b, created_at="now() - interval '5 minutes'")

    holder = sync_engine.connect()
    try:
        holder.execute(text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": locked})

        started = time.monotonic()
        claimed = worker_job.claim_next("w-skip")
        elapsed = time.monotonic() - started

        assert claimed is not None, "claim returned nothing instead of skipping the locked row"
        assert claimed.job_id == available, "claim did not skip the locked row"
        assert elapsed < 2.0, f"claim blocked for {elapsed:.1f}s — that is not SKIP LOCKED"
    finally:
        holder.rollback()
        holder.close()


def test_claim_increments_attempts_exactly_once(
    worker_db: Engine,
    make_user: Callable[..., uuid.UUID],
    make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    """``attempts`` is the retry budget; a double increment silently halves it."""
    jid = make_job(make_user(), attempts=1)

    claimed = worker_job.claim_next("w1")

    assert claimed is not None and claimed.job_id == jid
    row = job_state(jid, "attempts", "state", "worker_id", "started_at")
    assert row["attempts"] == 2
    assert row["state"] == "running"
    assert row["worker_id"] == "w1"
    assert row["started_at"] is not None


def test_empty_queue_returns_none(worker_db: Engine) -> None:
    assert worker_job.claim_next("w1") is None


def test_per_user_running_cap_is_enforced(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """One user cannot occupy two GPU slots at ``WORKER_MAX_RUNNING_PER_USER = 1``."""
    assert worker_settings.WORKER_MAX_RUNNING_PER_USER == 1
    user = make_user()
    make_job(user, state="running")
    make_job(user, state="queued")

    assert worker_job.claim_next("w1") is None, (
        "a user already on the GPU had a second job dispatched"
    )


def test_terminal_and_awaiting_upload_jobs_are_never_claimed(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """Only 'queued' is dispatchable. An awaiting_upload job has no bytes yet."""
    for state in ("awaiting_upload", "done", "failed", "cancelled", "expired"):
        make_job(make_user(), state=state)

    assert worker_job.claim_next("w1") is None


def test_claim_is_ordered_oldest_first(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """Within one priority band and one fair_rank, oldest-queued wins.

    Both timestamps are set together. An earlier version of this test set only
    ``created_at`` and left ``queued_at`` at the factory default of ``now()``, which
    passed under the old ``ORDER BY created_at`` and is meaningless under the new key —
    all three rows became tied. That is the behaviour
    ``test_a_slow_upload_does_not_reserve_a_queue_slot`` asserts deliberately.
    """
    def at(minutes: int, user) -> uuid.UUID:
        stamp = f"now() - interval '{minutes} minutes'"
        return make_job(user, created_at=stamp, queued_at=stamp)

    newest = at(1, make_user())
    oldest = at(30, make_user())
    middle = at(10, make_user())

    order = [worker_job.claim_next(f"w{i}") for i in range(3)]
    assert [c.job_id for c in order if c] == [oldest, middle, newest]


# ------------------------------------------------- what Phase 4 is supposed to fix


def test_one_user_cannot_occupy_every_queue_position(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """The starvation bug, stated as the behaviour we want.

    User A submits their allowance (5) first; user B submits one job afterwards. B
    must not wait behind all five. The desired dispatch order interleaves:
    ``A1, B1, A2, ...`` — so B's job is *second*, not sixth.
    """
    user_a, user_b = make_user(), make_user()
    # fair_rank is what admit() assigns from the user's own queue depth: A's five jobs
    # are their 1st..5th waiting job, B's is their 1st.
    #
    # All timestamps are inside WORKER_QUEUE_AGING_SECONDS (600 s) so the aging clause
    # is not what decides this — otherwise the test would pass for the wrong reason.
    a_jobs = [
        make_job(user_a, created_at=f"now() - interval '{300 - i * 10} seconds'",
                 queued_at=f"now() - interval '{300 - i * 10} seconds'",
                 fair_rank=i)
        for i in range(5)
    ]
    b_job = make_job(user_b, created_at="now() - interval '60 seconds'",
                     queued_at="now() - interval '60 seconds'", fair_rank=0)

    # Drain the queue. The per-user cap means a claimed job must be retired before
    # that user's next one is eligible, which is also how a real worker behaves.
    order: list[uuid.UUID] = []
    for i in range(6):
        claimed = worker_job.claim_next(f"w{i}")
        assert claimed is not None, f"queue dried up after {i} claims"
        order.append(claimed.job_id)
        worker_job.finish_success(
            claimed.job_id, result_key="k", mask_key=None, gpu_seconds=0.1, message="done"
        )

    assert order[0] == a_jobs[0], "the earliest submission should still go first"
    assert order[1] == b_job, (
        f"user B waited {order.index(b_job)} jobs behind user A instead of 1 — "
        "one tenant is monopolising the queue"
    )


def test_a_slow_upload_does_not_reserve_a_queue_slot(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """``created_at`` is stamped at reservation, ``queued_at`` when work is real.

    A client that POSTs a job and then uploads for twenty minutes should not
    outrank work that became runnable in the meantime. Ordering by ``created_at``
    lets it, which is a free queue-jumping primitive.
    """
    slow_uploader, prompt_user = make_user(), make_user()
    # Reserved 20 min ago, only finished uploading just now.
    late = make_job(
        slow_uploader,
        created_at="now() - interval '20 minutes'",
        queued_at="now() - interval '10 seconds'",
    )
    # Reserved and queued 5 min ago, i.e. runnable for far longer.
    ready = make_job(
        prompt_user,
        created_at="now() - interval '5 minutes'",
        queued_at="now() - interval '5 minutes'",
    )

    claimed = worker_job.claim_next("w1")
    assert claimed is not None
    assert claimed.job_id == ready, (
        "a job that spent 20 minutes uploading jumped ahead of one that had been "
        f"runnable for 5 (got {claimed.job_id}, wanted {ready})"
    )
    assert late is not None  # referenced for clarity

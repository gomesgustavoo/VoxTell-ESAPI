"""The dispatch ordering matrix.

Four clauses, and the tests here pin the *precedence* between them, not just each in
isolation — precedence is where a policy silently drifts. The order is:

    1. aging  (COALESCE(queued_at, created_at) older than WORKER_QUEUE_AGING_SECONDS)
    2. priority DESC        (from the user's plan, copied at enqueue)
    3. fair_rank            (the user's own queue depth at enqueue)
    4. COALESCE(queued_at, created_at)

``test_aging_beats_priority`` records a contestable business decision: a long-waiting
free-tier job outranks a brand-new paid one. The reasoning is that unbounded starvation
of a clinical user is a worse product outcome than a paying user waiting for one extra
job. **If the business reverses that, swap clauses 1 and 2 in ``_CLAIM_SQL`` and invert
this test in the same commit** — the whole point of having it is that the policy cannot
change by accident.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest
from sqlalchemy.engine import Engine

from worker import job as worker_job
from worker.settings import settings as ws

pytestmark = pytest.mark.pg

# Comfortably inside the aging window, so clause 1 never fires unless a test wants it.
FRESH = "now() - interval '30 seconds'"
# Comfortably outside it.
AGED = f"now() - interval '{ws.WORKER_QUEUE_AGING_SECONDS + 300} seconds'"


def _drain(n: int) -> list[uuid.UUID]:
    """Claim n jobs, retiring each so the per-user running cap does not block."""
    out: list[uuid.UUID] = []
    for i in range(n):
        claimed = worker_job.claim_next(f"w{i}")
        if claimed is None:
            break
        out.append(claimed.job_id)
        worker_job.finish_success(
            claimed.job_id, result_key="k", mask_key=None, gpu_seconds=0.1, message="ok"
        )
    return out


def test_round_robin_interleaves_two_users(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """A×3 then B×3, all fresh: dispatch must alternate, not drain A first."""
    a, b = make_user(), make_user()
    a_jobs = [make_job(a, created_at=FRESH, queued_at=FRESH, fair_rank=i) for i in range(3)]
    b_jobs = [make_job(b, created_at=FRESH, queued_at=FRESH, fair_rank=i) for i in range(3)]

    order = _drain(6)
    owners = ["A" if j in a_jobs else "B" for j in order]

    assert owners == ["A", "B", "A", "B", "A", "B"], (
        f"expected round-robin, got {''.join(owners)}"
    )


def test_fair_rank_outranks_the_timestamp(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """A newer rank-0 job beats an older rank-1 job. This is the whole mechanism."""
    a, b = make_user(), make_user()
    a_second = make_job(a, created_at=AGED, queued_at="now() - interval '200 seconds'",
                        fair_rank=1)
    b_first = make_job(b, created_at=FRESH, queued_at=FRESH, fair_rank=0)

    claimed = worker_job.claim_next("w")
    assert claimed is not None and claimed.job_id == b_first, (
        "a user's second queued job outranked another user's first"
    )
    assert a_second is not None


def test_priority_band_jumps_the_queue(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """A higher plan priority wins, even at a worse fair_rank and later timestamp."""
    free, paid = make_user(), make_user()
    make_job(free, created_at=FRESH, queued_at=FRESH, fair_rank=0, priority=100)
    premium = make_job(paid, created_at=FRESH, queued_at=FRESH, fair_rank=3, priority=200)

    claimed = worker_job.claim_next("w")
    assert claimed is not None and claimed.job_id == premium


def test_paid_users_still_round_robin_among_themselves(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """Priority selects a band; fairness still applies inside it."""
    a, b = make_user(), make_user()
    a_jobs = [make_job(a, created_at=FRESH, queued_at=FRESH, fair_rank=i, priority=200)
              for i in range(2)]
    b_jobs = [make_job(b, created_at=FRESH, queued_at=FRESH, fair_rank=i, priority=200)
              for i in range(2)]

    owners = ["A" if j in a_jobs else "B" for j in _drain(4)]
    assert owners == ["A", "B", "A", "B"], f"got {''.join(owners)}"


def test_aging_beats_priority(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """A CONTESTABLE POLICY DECISION, pinned on purpose.

    A free-tier job that has waited past the aging window outranks a brand-new paid
    job. Rationale: starving a clinical user indefinitely is worse than a paying user
    waiting for one more job. To reverse it, swap the first two ORDER BY clauses in
    ``worker/job.py::_CLAIM_SQL`` and invert this assertion together.
    """
    free, paid = make_user(), make_user()
    starving = make_job(free, created_at=AGED, queued_at=AGED, fair_rank=0, priority=100)
    make_job(paid, created_at=FRESH, queued_at=FRESH, fair_rank=0, priority=200)

    claimed = worker_job.claim_next("w")
    assert claimed is not None and claimed.job_id == starving, (
        "a job past the aging window did not outrank a fresh higher-priority job — "
        "if that is the intended policy now, swap clauses 1 and 2 of _CLAIM_SQL"
    )


def test_not_before_defers_a_retried_job(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """Backoff must actually hold the job back, and only until its time.

    ``not_before`` is set through SQL rather than a bound Python datetime, because the
    predicate is evaluated against the database's ``now()`` — see tests/conftest.py.
    """
    from sqlalchemy import text

    deferred = make_job(make_user(), created_at=AGED, queued_at=AGED, fair_rank=0)

    def set_not_before(expr: str) -> None:
        with worker_db.begin() as conn:
            conn.execute(
                text(f"UPDATE jobs SET not_before = {expr} WHERE id = :j"), {"j": deferred}
            )

    set_not_before("now() + interval '10 minutes'")
    assert worker_job.claim_next("w") is None, "a job still in backoff was dispatched"

    set_not_before("now() - interval '1 second'")
    claimed = worker_job.claim_next("w")
    assert claimed is not None and claimed.job_id == deferred, (
        "a job whose backoff has elapsed was not dispatched"
    )


def test_claim_sets_both_clocks(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID],
    job_state: Callable[..., dict],
) -> None:
    """Every claimed row must carry a lease AND a deadline.

    This is also what the ``ck_jobs_running_has_lease`` CHECK constraint enforces at the
    schema level, so a claim path that forgets one becomes structurally impossible.
    """
    jid = make_job(make_user(), created_at=FRESH, queued_at=FRESH)

    claimed = worker_job.claim_next("w")
    assert claimed is not None and claimed.job_id == jid

    row = job_state(jid, "lease_expires_at", "deadline_at", "not_before", "attempts")
    assert row["lease_expires_at"] is not None
    assert row["deadline_at"] is not None
    assert row["not_before"] is None, "claiming must clear any backoff marker"
    assert row["attempts"] == 1


def test_the_lease_constraint_is_enforced(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """ck_jobs_running_has_lease makes a lease-less claim path impossible to ship.

    Without it, a future refactor that dropped ``lease_expires_at`` from the claim would
    silently restore the old behaviour: a running job with no expiry, unreclaimable, and
    able to hold the cross-product GPU mutex indefinitely.
    """
    import pytest as _pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    jid = make_job(make_user(), created_at=FRESH, queued_at=FRESH)

    with _pytest.raises(IntegrityError, match="ck_jobs_running_has_lease"):
        with worker_db.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET state = 'running' WHERE id = :j"), {"j": jid}
            )


def test_claim_reports_attempts_for_backoff(
    worker_db: Engine, make_user: Callable[..., uuid.UUID], make_job: Callable[..., uuid.UUID]
) -> None:
    """The retry delay grows from the ROW's history, not from process memory."""
    make_job(make_user(), created_at=FRESH, queued_at=FRESH, attempts=2)
    claimed = worker_job.claim_next("w")
    assert claimed is not None
    assert claimed.attempts == 3, "attempts must be the post-increment value from the claim"

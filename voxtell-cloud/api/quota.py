"""Admission control: per-user outstanding-job cap and monthly quota.

One GPU serves everyone, so the interesting limit is not requests-per-second
(Traefik handles that) but *how much of the queue one user may hold*. Two gates:

* **Outstanding jobs** — ``queued + running`` may not exceed
  ``MAX_RUNNING_PER_USER + MAX_QUEUED_PER_USER``. Exceeding it is a 429 with
  ``Retry-After``, not a failure: the client backs off and resubmits.
  (The *running* cap itself is enforced where it belongs — in the worker's claim
  query, which will not pick a second job for a user who already has one on the
  GPU. With a single worker that is automatic, but it keeps the scheduler fair
  if we ever add a second GPU.)
* **Monthly quota** — jobs *submitted* per calendar month (UTC), counted from
  ``usage_events``. Counting submissions rather than completions means a user
  cannot burn GPU time repeatedly by cancelling.
* **Global queue depth** — the total backlog across every user. The per-user caps
  bound one tenant and nothing bounded the sum, so twenty users at six outstanding
  each is 120 queued jobs, every volume-backed one of which pins its ``Volume``
  alive against a 50 Gi bucket. This gate returns ``queue_full``, distinct from
  ``too_many_outstanding_jobs``, because the honest message is "the service is
  busy", not "you have too many jobs".

``admit()`` also assigns the new job's **fair_rank** — the caller reads
``state.queued`` and stamps it on the job. That has to happen here rather than in
the route, because the value is only meaningful under the user-row lock this
function already takes; recounting afterwards lets two concurrent submits take the
same rank.

``Retry-After`` on both 429s is **measured**, not hardcoded. See ``estimate.py``:
the old constant 30 was wrong by an order of magnitude on a deep queue, and the
ESAPI client honours the header, so it produced a re-POST storm that looked to the
planner like a broken service.

Both gates are checked when a job is **submitted**, not when it is created. That
is a change: they used to run at create, which meant a job merely waiting for
bytes held a GPU slot and had already spent a quota unit, so a run of failed
uploads could lock an account out of the GPU entirely. Create still performs a
*non-binding* read-only quota look so a client is warned before uploading tens of
megabytes it cannot use.

Counting happens under ``SELECT ... FOR UPDATE`` on the user row, so two
simultaneous submissions from one user cannot both see room for the last slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .errors import quota_exceeded, too_many_requests
from .estimate import wait_estimate_seconds
from .metrics import observe_rejection
from .models import Job, UsageEvent, User

# States that still consume a slot in the user's outstanding budget.
#
# `awaiting_upload` is deliberately NOT here, and must not be added back. It was,
# and the consequence was a lockout: the cap is 6, an abandoned upload is only
# reaped after VOXTELL_UPLOAD_TTL_MINUTES (120), so six failed uploads inside two
# hours made every subsequent create a 429 — advertising `Retry-After: 30`, wrong
# by two orders of magnitude. A job waiting for bytes is not competing for the
# GPU. Open uploads are bounded separately by
# VOXTELL_MAX_AWAITING_UPLOAD_PER_USER, which limits storage.
#
# tests/test_quota_states.py pins this.
OUTSTANDING_STATES = ("queued", "running")


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class QuotaState:
    used: int
    limit: int | None
    # Split from a single ``outstanding`` count because the two numbers now have
    # different jobs. The sum is still the admission gate, but ``queued`` on its own
    # is what a new job's ``fair_rank`` is set from — it is literally "how many of
    # this user's jobs are already waiting".
    queued: int
    running: int
    max_outstanding: int

    @property
    def outstanding(self) -> int:
        return self.queued + self.running

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)


def _max_outstanding() -> int:
    return settings.VOXTELL_MAX_RUNNING_PER_USER + settings.VOXTELL_MAX_QUEUED_PER_USER


async def load_state(session: AsyncSession, user: User) -> QuotaState:
    used = await session.scalar(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= month_start(),
        )
    )
    queued = await session.scalar(
        select(func.count(Job.id)).where(Job.user_id == user.id, Job.state == "queued")
    )
    running = await session.scalar(
        select(func.count(Job.id)).where(Job.user_id == user.id, Job.state == "running")
    )
    return QuotaState(
        used=int(used or 0),
        limit=user.monthly_job_quota,
        queued=int(queued or 0),
        running=int(running or 0),
        max_outstanding=_max_outstanding(),
    )


async def global_queue_depth(session: AsyncSession) -> int:
    """Queued jobs across every user — the shared-GPU backlog."""
    depth = await session.scalar(select(func.count(Job.id)).where(Job.state == "queued"))
    return int(depth or 0)


async def admit(session: AsyncSession, user: User) -> QuotaState:
    """Check every gate; raise 402 / 429 on refusal, else return the state.

    The returned state's ``queued`` is the caller's ``fair_rank`` for the job it is
    about to enqueue — read it under the same lock this function already holds, and
    do not recount afterwards, or two concurrent submits can both take rank *n*.

    Does not record usage — the caller creates the ``UsageEvent`` once it has a job
    id, inside the same transaction, so the count and the job commit or roll back
    together.
    """
    # Serialise concurrent submissions from this user behind the user row. This is
    # what makes both the last-free-slot check and the fair_rank assignment atomic.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())

    state = await load_state(session, user)

    if state.limit is not None and state.used >= state.limit:
        observe_rejection("monthly_quota_exceeded")
        raise quota_exceeded(used=state.used, limit=state.limit)

    if state.outstanding >= state.max_outstanding:
        retry_after = await wait_estimate_seconds(session, state.queued)
        observe_rejection("too_many_outstanding_jobs", retry_after)
        raise too_many_requests(
            "too_many_outstanding_jobs",
            f"You already have {state.outstanding} job(s) in flight "
            f"(limit {state.max_outstanding}). Wait for one to finish.",
            retry_after=retry_after,
        )

    # Global backlog, checked last: a user inside their own allowance should be
    # told "the service is busy", not "you have too many jobs".
    depth = await global_queue_depth(session)
    if depth >= settings.VOXTELL_MAX_GLOBAL_QUEUED:
        retry_after = await wait_estimate_seconds(session, depth)
        observe_rejection("queue_full", retry_after)
        raise too_many_requests(
            "queue_full",
            f"The segmentation queue is full ({depth} job(s) waiting). "
            "Your work has not been lost — resubmit shortly.",
            retry_after=retry_after,
        )

    return state

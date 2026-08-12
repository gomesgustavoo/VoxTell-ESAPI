"""Queue operations: claim, heartbeat, progress, finish, requeue.

Raw SQL on the sync engine. Claiming uses ``FOR UPDATE SKIP LOCKED`` so N worker
replicas never double-claim the same row. A ``heartbeat_at`` column makes pod
deaths self-healing: the stale sweep requeues jobs whose worker stopped
heartbeating, up to ``WORKER_MAX_ATTEMPTS``, then fails them terminally.

This mirrors the queue that already runs DicomSegVR's GPU pipeline on this
cluster — same locking, same heartbeat semantics, same throttled progress
writer — because that design has proven itself against real pod restarts.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from . import db, metrics
from .settings import settings

log = logging.getLogger("worker.job")


@dataclass
class ClaimedJob:
    job_id: uuid.UUID
    user_id: uuid.UUID
    prompts: list[str]
    geometry: dict[str, Any]
    volume_key: str
    keep_largest: bool
    want_mask: bool
    # NULL for a legacy inline upload, set when the job segments a shared volume
    # from POST /v1/volumes. The worker deletes its input only in the first case:
    # a shared volume belongs to the user, not to this job, and deleting it would
    # break the very next prompt they try. See owns_volume.
    volume_id: Optional[uuid.UUID] = None
    # Post-increment attempt count from the claim, so the retry backoff grows with the
    # row's own history rather than with anything this process remembers.
    attempts: int = 1

    @property
    def affine_lps(self) -> list[list[float]]:
        return self.geometry["affine_lps"]

    @property
    def owns_volume(self) -> bool:
        """True when this job uploaded its own input and should delete it."""
        return self.volume_id is None

    @property
    def expected_content_sha256(self) -> Optional[str]:
        """Digest the uploaded voxels must match, when the API recorded one.

        Verified in the worker rather than at upload time because the bytes are
        already decompressed and in memory here — checking it in the API would
        mean pulling tens of megabytes back through a process whose defining
        property is that the volume never passes through it.

        This is a patient-safety check, not hygiene: a wrong hash means the dedup
        lookup could serve the wrong series, and a planner would receive contours
        for a CT that is not the one on their screen.
        """
        return self.geometry.get("content_sha256")


# Dispatch. Four ordering clauses, outermost first:
#
#   1. AGING     — a job that has waited past WORKER_QUEUE_AGING_SECONDS jumps every
#                  priority band. Deliberately ranked above priority: unbounded
#                  starvation of a clinical user is worse than a paying user waiting
#                  for one extra job. Swap clauses 1 and 2 to reverse the policy, and
#                  swap tests/test_claim_fairness.py with it.
#   2. PRIORITY  — copied from the user's plan at enqueue, so a mid-session plan
#                  change never reshuffles work that is already waiting.
#   3. FAIR_RANK — the user's own queue depth when this job was enqueued. THIS is what
#                  turns global FIFO into round-robin: user A's sixth job (rank 5)
#                  sorts behind user B's first (rank 0), so one tenant can no longer
#                  hold every position. Computed under the user-row lock admit()
#                  already takes, so it cannot be gamed by concurrent submits.
#   4. FIFO      — COALESCE(queued_at, created_at). queued_at, because created_at is
#                  stamped at *reservation*: ordering by it let a client POST a job,
#                  upload for twenty minutes, and still outrank work that became
#                  runnable in the meantime. COALESCE rather than a bare queued_at
#                  because NULL sorts LAST in ASC, which would silently park a
#                  historic row at the back of the queue forever.
#
# All four are plain columns, which is the whole reason fair_rank is stored rather than
# computed: **Postgres rejects FOR UPDATE in any query containing a window function**,
# so the natural `row_number() OVER (PARTITION BY user_id)` is simply unavailable here,
# and restructuring into CTE-ranks-then-lock loses the ordering across the join and
# reintroduces the double-claim race SKIP LOCKED exists to prevent.
_CLAIM_SQL = text(
    """
    UPDATE jobs
       SET state = 'running',
           started_at = now(),
           heartbeat_at = now(),
           lease_expires_at = now() + make_interval(secs => :lease_secs),
           deadline_at = now() + make_interval(secs => :timeout_secs),
           not_before = NULL,
           attempts = attempts + 1,
           worker_id = :wid,
           message = 'Starting',
           progress = 0
     WHERE id = (
            SELECT j.id FROM jobs j
             WHERE j.state = 'queued'
               AND (j.not_before IS NULL OR j.not_before <= now())
               AND (SELECT count(*) FROM jobs r
                     WHERE r.user_id = j.user_id
                       AND r.state = 'running') < :max_per_user
             ORDER BY
               (COALESCE(j.queued_at, j.created_at)
                  < now() - make_interval(secs => :aging_secs)) DESC,
               j.priority DESC,
               j.fair_rank,
               COALESCE(j.queued_at, j.created_at)
             LIMIT 1
               FOR UPDATE SKIP LOCKED
           )
 RETURNING id, user_id, prompts, geometry, volume_key, keep_largest, want_mask,
           volume_id, queued_at, started_at, attempts
    """
)

# Lease-based recovery, mirroring api/reclaim.py. The API runs this every 30 s and is
# the primary path — it is the thing that is still up when the worker is not. This copy
# exists so a worker with an unreachable API still self-heals, and is throttled to
# WORKER_SWEEP_INTERVAL_SECONDS rather than running on every poll (it used to run twice
# per ~5 s, i.e. ~34k pointless write transactions a day).
#
# queued_at is deliberately NOT reset: an interrupted job has already waited, and
# keeping the timestamp is what lets clause 1 above promote it rather than sending it
# to the back of the line.
_REQUEUE_SQL = text(
    """
    UPDATE jobs
       SET state = 'queued', worker_id = NULL, progress = 0,
           lease_expires_at = NULL, deadline_at = NULL,
           message = 'Requeued after the worker stopped responding'
     WHERE state = 'running'
       AND lease_expires_at IS NOT NULL
       AND lease_expires_at < now()
       AND attempts < :max_attempts
 RETURNING id
    """
)

_FAIL_STALE_SQL = text(
    """
    UPDATE jobs
       SET state = 'failed', finished_at = now(), failure_class = 'stalled',
           message = 'Failed',
           error = 'The worker stopped responding repeatedly; giving up'
     WHERE state = 'running'
       AND lease_expires_at IS NOT NULL
       AND lease_expires_at < now()
       AND attempts >= :max_attempts
 RETURNING id
    """
)

_FAIL_DEADLINE_SQL = text(
    """
    UPDATE jobs
       SET state = 'failed', finished_at = now(), failure_class = 'timeout',
           message = 'Failed',
           error = 'Exceeded the maximum run time; the job was stopped'
     WHERE state = 'running'
       AND deadline_at IS NOT NULL
       AND deadline_at < now()
 RETURNING id
    """
)

# Renewal is conditional on observed progress — see worker/watchdog.py. The state
# guard means a job cancelled or reclaimed underneath us is not resurrected.
_RENEW_LEASE_SQL = text(
    """
    UPDATE jobs
       SET lease_expires_at = now() + make_interval(secs => :lease_secs),
           heartbeat_at = now()
     WHERE id = :jid AND state = 'running'
    """
)


def claim_next(worker_id: str) -> Optional[ClaimedJob]:
    started = time.monotonic()
    with db.get_engine().begin() as conn:
        row = conn.execute(
            _CLAIM_SQL,
            {
                "wid": worker_id,
                "max_per_user": settings.WORKER_MAX_RUNNING_PER_USER,
                "lease_secs": settings.WORKER_LEASE_SECONDS,
                "timeout_secs": settings.WORKER_JOB_TIMEOUT_SECONDS,
                "aging_secs": settings.WORKER_QUEUE_AGING_SECONDS,
            },
        ).first()
    metrics.CLAIM_SECONDS.observe(time.monotonic() - started)
    if row is None:
        metrics.CLAIMS.labels(result="empty").inc()
        return None
    metrics.CLAIMS.labels(result="job").inc()

    # started_at - queued_at, measured at the moment of dispatch: the fairness SLI.
    # Taken from the RETURNING row rather than a Python clock so it is the database's
    # own view of both timestamps.
    queued_at, started_at = row[8], row[9]
    if queued_at is not None and started_at is not None:
        metrics.QUEUE_WAIT.observe(max(0.0, (started_at - queued_at).total_seconds()))

    return ClaimedJob(
        job_id=row[0],
        user_id=row[1],
        prompts=list(row[2] or []),
        geometry=dict(row[3] or {}),
        volume_key=row[4],
        keep_largest=bool(row[5]),
        want_mask=bool(row[6]),
        volume_id=row[7],
        attempts=int(row[10] or 1),
    )


def sweep_stale() -> None:
    """Recover jobs whose lease lapsed, and fail those out of budget or over time.

    A fallback for the API's reclaim loop, which is the primary path. Deliberately the
    same three statements in the same order — deadline first, so a job that is both
    over its deadline and past its lease fails terminally instead of going round again.
    """
    params = {"max_attempts": settings.WORKER_MAX_ATTEMPTS}
    with db.get_engine().begin() as conn:
        timed_out = conn.execute(_FAIL_DEADLINE_SQL).fetchall()
        failed = conn.execute(_FAIL_STALE_SQL, params).fetchall()
        requeued = conn.execute(_REQUEUE_SQL, params).fetchall()
    for rows, action, level in (
        (requeued, "lease_expired_requeued", log.warning),
        (failed, "attempts_exhausted_failed", log.error),
        (timed_out, "deadline_failed", log.error),
    ):
        if rows:
            metrics.RECLAIM_ACTIONS.labels(action=action).inc(len(rows))
            level("%s: %d job(s): %s", action, len(rows), [str(r[0]) for r in rows])


def renew_lease(job_id: uuid.UUID) -> None:
    """Extend the lease. Called ONLY for jobs observed to be making progress."""
    with db.get_engine().begin() as conn:
        conn.execute(
            _RENEW_LEASE_SQL,
            {"jid": job_id, "lease_secs": settings.WORKER_LEASE_SECONDS},
        )


def heartbeat(job_id: uuid.UUID) -> None:
    """Refresh heartbeat_at only. Retained for observability; the lease is authority."""
    with db.get_engine().begin() as conn:
        conn.execute(
            text("UPDATE jobs SET heartbeat_at = now() WHERE id = :jid AND state = 'running'"),
            {"jid": job_id},
        )


def cancel_requested(job_id: uuid.UUID) -> bool:
    with db.get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT cancel_requested FROM jobs WHERE id = :jid"), {"jid": job_id}
        ).first()
    return bool(row and row[0])


class CancelWatcher:
    """Cancellation check, polled at most once every ``interval`` seconds.

    The sliding-window callback fires once per patch — hundreds of times a
    minute — so an unthrottled check would put a SELECT between every patch.
    Two seconds of latency on a cancel is imperceptible next to a job that runs
    for minutes. Once true, it stays true without further queries.
    """

    def __init__(self, job_id: uuid.UUID, interval: float = 2.0) -> None:
        self._jid = job_id
        self._interval = interval
        self._last = 0.0
        self._cancelled = False

    def __call__(self) -> bool:
        if self._cancelled:
            return True
        now = time.monotonic()
        if (now - self._last) < self._interval:
            return False
        self._last = now
        try:
            self._cancelled = cancel_requested(self._jid)
        except Exception as exc:
            # A transient database blip must not abort a long GPU job.
            log.warning("cancel check failed: %s", exc)
            return False
        return self._cancelled


def finish_success(
    job_id: uuid.UUID,
    *,
    result_key: str,
    mask_key: str | None,
    gpu_seconds: float,
    message: str,
) -> bool:
    """Record a completed job. Returns False if it had been cancelled instead.

    The ``AND NOT cancel_requested`` guard below is defence in depth for a bug that was
    live: a cancel arriving after ``engine.segment`` returned normally was never
    re-read, so the job was written ``done``, a result was uploaded, and a UsageEvent
    was charged for work the user had explicitly cancelled. ``worker/main.py`` now
    checks at three boundaries — but those are three call sites a future refactor could
    quietly drop, whereas this is one statement that cannot be bypassed.

    Deliberately a guarded UPDATE rather than a CHECK constraint on the table: a
    constraint would turn the race into a failed transaction, leaving the row in
    ``running`` with its result orphaned until reclaim. Here the caller simply learns
    the truth and records the cancellation.
    """
    with db.get_engine().begin() as conn:
        updated = conn.execute(
            text(
                """
                UPDATE jobs
                   SET state = 'done', progress = 1, message = :msg,
                       result_key = :rkey, mask_key = :mkey,
                       gpu_seconds = :gpu, finished_at = now(), error = NULL,
                       -- Clear both clocks. The reclaim queries are guarded on
                       -- state = 'running' so a finished row was never at risk, but
                       -- leaving a lapsed lease behind makes
                       -- voxtell_jobs_lease_expired_current ambiguous and invites a
                       -- future query to forget the state guard.
                       lease_expires_at = NULL, deadline_at = NULL
                 WHERE id = :jid
                   AND NOT cancel_requested
                """
            ),
            {
                "jid": job_id,
                "msg": message[:2000],
                "rkey": result_key,
                "mkey": mask_key,
                "gpu": gpu_seconds,
            },
        ).rowcount
        if not updated:
            # The user cancelled while this job was finishing. Honour that: no
            # result_key, so nothing is offered for download, and no gpu_seconds
            # backfilled onto the usage row.
            conn.execute(
                text(
                    "UPDATE jobs SET state = 'cancelled', message = 'Cancelled', "
                    "lease_expires_at = NULL, deadline_at = NULL, finished_at = now() "
                    "WHERE id = :jid AND state = 'running'"
                ),
                {"jid": job_id},
            )
            log.info("job %s completed but had been cancelled; recorded as cancelled", job_id)
            return False
        # Backfill the usage row created at submission with what it actually cost.
        conn.execute(
            text("UPDATE usage_events SET gpu_seconds = :gpu WHERE job_id = :jid"),
            {"jid": job_id, "gpu": gpu_seconds},
        )
    return True


def finish_failure(job_id: uuid.UUID, error: str, failure_class: str = "permanent") -> None:
    with db.get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET state = 'failed', error = :err, message = 'Failed', "
                "failure_class = :fc, lease_expires_at = NULL, deadline_at = NULL, "
                "finished_at = now() WHERE id = :jid"
            ),
            {"jid": job_id, "err": error[:2000], "fc": failure_class},
        )


def finish_transient_failure(job_id: uuid.UUID, error: str, delay_seconds: float) -> bool:
    """Requeue after a retryable failure, or fail terminally if out of attempts.

    Returns True when the job went back on the queue. The attempts check is done in
    SQL against the row's own counter rather than from a value the caller carries, so
    two workers cannot each decide there is one attempt left.

    ``queued_at`` is left alone on purpose — the job has already waited once, and
    keeping it is what lets the claim's aging clause promote it.
    """
    with db.get_engine().begin() as conn:
        requeued = conn.execute(
            text(
                """
                UPDATE jobs
                   SET state = 'queued', worker_id = NULL, progress = 0,
                       lease_expires_at = NULL, deadline_at = NULL,
                       failure_class = 'transient',
                       not_before = now() + make_interval(secs => :delay),
                       message = 'Retrying after a temporary problem',
                       error = :err
                 WHERE id = :jid AND state = 'running' AND attempts < :max_attempts
             RETURNING id
                """
            ),
            {
                "jid": job_id,
                "err": error[:2000],
                "delay": float(delay_seconds),
                "max_attempts": settings.WORKER_MAX_ATTEMPTS,
            },
        ).first()
    if requeued is not None:
        log.warning(
            "job %s hit a temporary problem, retrying in %.0fs: %s",
            job_id, delay_seconds, error[:200],
        )
        return True
    # Out of attempts: the failure was transient in kind but has now happened enough
    # times to be the user's problem rather than ours.
    finish_failure(
        job_id,
        f"Repeated temporary failures, giving up. Last error: {error}",
        failure_class="transient",
    )
    return False


def finish_cancelled(job_id: uuid.UUID) -> None:
    with db.get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET state = 'cancelled', message = 'Cancelled', "
                "lease_expires_at = NULL, deadline_at = NULL, "
                "finished_at = now() WHERE id = :jid"
            ),
            {"jid": job_id},
        )


class ProgressReporter:
    """Throttled progress writer: at most one row update per 2 s or 2 % of delta.

    Called from the sliding-window callback, which fires once per patch — that
    can be hundreds of times a minute, so unthrottled it would be a write storm.
    Every write also refreshes ``heartbeat_at``, so an active job can never be
    swept as stale.

    **Touches the lease on EVERY call, before the throttle.** This is the primary
    source of "the job is progressing" evidence, and it must not inherit the write
    throttle: the throttle exists to protect Postgres, whereas the lease is in-process
    and free. Touching only on the writes that get through would make a job that is
    progressing steadily but slowly look stalled.
    """

    def __init__(
        self,
        job_id: uuid.UUID,
        min_interval: float = 2.0,
        min_delta: float = 0.02,
        lease=None,
    ) -> None:
        self._jid = job_id
        self._min_interval = min_interval
        self._min_delta = min_delta
        self._last_time = 0.0
        self._last_frac = -1.0
        self._lease = lease

    def __call__(self, frac: float, message: str) -> None:
        if self._lease is not None:
            self._lease.touch()
        now = time.monotonic()
        if (now - self._last_time) < self._min_interval and (frac - self._last_frac) < self._min_delta:
            return
        self._last_time = now
        self._last_frac = frac
        try:
            with db.get_engine().begin() as conn:
                conn.execute(
                    text(
                        "UPDATE jobs SET progress = :p, message = :msg, heartbeat_at = now() "
                        "WHERE id = :jid AND state = 'running'"
                    ),
                    {
                        "jid": self._jid,
                        "p": min(0.999, max(0.0, float(frac))),
                        "msg": message[:2000],
                    },
                )
        except Exception as exc:  # a progress write must never kill a job
            log.warning("progress write failed: %s", exc)

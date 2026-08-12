"""How long a caller will actually wait — measured, not guessed.

Two consumers, both of which were previously lying to the client:

* ``Retry-After`` on the 429 from ``quota.admit()``, which was hardcoded to 30
  seconds. With twenty jobs queued at roughly a minute each the honest answer is
  twenty minutes. The ESAPI client *honours* the header, so the hardcoded value
  turned one full queue into a re-POST every 30 seconds — a self-inflicted retry
  storm against Traefik's rate limit, and a planner who concludes the service is
  broken rather than busy.
* ``estimated_wait_seconds`` on a job status, so the console and the plugin can
  show "about 4 minutes" instead of "6 jobs ahead" — a number a clinician can
  act on.

The service rate is the **median** of recent completions, not the mean: job
duration here is strongly right-skewed (measured 0.4 s to 76 s on the same GPU,
depending on prompt count and volume size) and one large study would drag a mean
far off. It is cached for ``VOXTELL_SERVICE_RATE_TTL_SECONDS`` because this feeds
a retry hint, so a minute of staleness costs nothing, while a percentile scan per
request would put avoidable work on the submit path.

The estimate assumes one job at a time, which is true: there is a single GPU and
``worker/engine.py`` serialises inference behind a mutex shared with DicomSegVR.
If a second GPU slot ever exists, divide by the slot count here — and nowhere
else.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings

log = logging.getLogger(__name__)

# Completions to sample. Big enough that one outlier does not move the median,
# small enough that the index scan stays trivial and the number tracks a change
# in workload within an hour rather than a week.
_SAMPLE_SIZE = 50

_MEDIAN_SQL = text(
    """
    SELECT percentile_cont(0.5) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))
           )
      FROM (
            SELECT started_at, finished_at FROM jobs
             WHERE state = 'done'
               AND started_at IS NOT NULL
               AND finished_at IS NOT NULL
             ORDER BY finished_at DESC
             LIMIT :sample
           ) recent
    """
)

# (monotonic_deadline, seconds). Process-local, so the two API replicas each keep
# their own — which is fine: they are estimating the same thing from the same rows
# and disagreeing by a few seconds is not observable.
_cache: tuple[float, float] | None = None


def _fallback() -> float:
    return float(settings.VOXTELL_DEFAULT_JOB_SECONDS)


async def service_rate_seconds(session: AsyncSession) -> float:
    """Median seconds a job spends on the GPU, from recent history."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now < _cache[0]:
        return _cache[1]

    try:
        median = await session.scalar(_MEDIAN_SQL, {"sample": _SAMPLE_SIZE})
    except Exception as exc:
        # An estimate is never worth failing a request over.
        log.warning("service-rate query failed, using default: %s", exc)
        return _fallback()

    # NULL when nothing has completed yet, and guard against a zero that would
    # make every wait estimate 0 and every Retry-After the floor.
    rate = float(median) if median else _fallback()
    if rate <= 0:
        rate = _fallback()

    _cache = (now + settings.VOXTELL_SERVICE_RATE_TTL_SECONDS, rate)
    return rate


def reset_cache() -> None:
    """Drop the memoised rate. Tests only."""
    global _cache
    _cache = None


def _clamp(seconds: float) -> int:
    floor = settings.VOXTELL_POLL_INTERVAL_SECONDS
    ceiling = settings.VOXTELL_MAX_RETRY_AFTER_SECONDS
    return int(max(floor, min(ceiling, round(seconds))))


async def wait_estimate_seconds(session: AsyncSession, jobs_ahead: int) -> int:
    """Rough seconds until a job with ``jobs_ahead`` in front of it starts.

    Clamped into ``[poll interval, max retry-after]``. The floor stops a client
    being told to come back sooner than its own poll cadence; the ceiling stops a
    deep queue producing a ``Retry-After`` so long the client looks hung — past
    ten minutes the honest advice is "check back later", not a precise number.
    """
    if jobs_ahead <= 0:
        return _clamp(0)
    return _clamp(max(0, jobs_ahead) * await service_rate_seconds(session))

"""How long a reusable volume lives, as pure arithmetic.

Split out of ``sweeper.py`` for one reason: this is the code that decides how long
patient voxels sit on disk, and it should be provable without a database. Every
function here takes its inputs explicitly and returns a value —
``tests/test_retention_policy.py`` exercises them directly.

The policy, and why it is not a refcount
----------------------------------------
The obvious lifetime rule for a shared object is "delete it when nothing
references it". That rule cannot work here: the references are jobs, and the
count reaches zero the instant the last job finishes — which *is* the behaviour
that made every prompt re-upload the series. A refcount can therefore only serve
as a safety interlock (never delete while a job is queued or running), and the
actual lifetime has to be time-based.

So: a sliding idle TTL under a hard age ceiling.

* ``VOXTELL_VOLUME_TTL_MINUTES`` slides forward on every use, so an operator
  working through a list of prompts keeps their upload alive.
* ``VOXTELL_VOLUME_MAX_AGE_HOURS`` never slides, so no amount of activity can
  keep a patient's CT resident indefinitely.

The privacy argument this supports
----------------------------------
The previous design deleted the input volume the moment a job reached a terminal
state, and that was deliberate. This feature genuinely weakens it, so the
reasoning is written down rather than assumed:

1. ``VOXTELL_VOLUME_MAX_AGE_HOURS <= VOXTELL_RESULT_TTL_HOURS``, so the input CT
   never outlives the contours derived from it and the platform's *maximum*
   patient-data retention is unchanged. :func:`max_age_within_result_ttl` makes
   that checkable rather than aspirational.
2. Point 1 is necessary but not sufficient — contours are sparse and derived,
   whereas a full head CT is re-identifiable. The rest of the argument is
   operational: ``DELETE /v1/volumes/{id}`` lets the operator purge before the
   TTL, the plugin releases on sign-out, ``GET /v1/volumes`` lets a data-protection
   officer see and purge what is held, and both numbers above are per-deployment
   so a site with a stricter agreement sets them lower.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import settings


def next_expiry(
    created_at: datetime,
    now: datetime,
    *,
    idle_minutes: int | None = None,
    max_age_hours: int | None = None,
) -> datetime:
    """When a volume touched at ``now`` should expire.

    ``min`` of the sliding idle window and the hard ceiling, so the return value
    is never later than ``created_at + max_age`` however often it is called.
    """
    idle = idle_minutes if idle_minutes is not None else settings.VOXTELL_VOLUME_TTL_MINUTES
    ceiling_hours = (
        max_age_hours if max_age_hours is not None else settings.VOXTELL_VOLUME_MAX_AGE_HOURS
    )

    idle_expiry = now + timedelta(minutes=idle)
    hard_ceiling = created_at + timedelta(hours=ceiling_hours)
    return min(idle_expiry, hard_ceiling)


def is_expired(expires_at: datetime, now: datetime) -> bool:
    return expires_at <= now


def max_age_within_result_ttl(
    max_age_hours: int | None = None, result_ttl_hours: int | None = None
) -> bool:
    """The invariant point 1 of the privacy argument rests on.

    Kept as a function rather than a comment so a test can fail when someone
    raises the volume ceiling past the result TTL, which would make the input CT
    outlive the contours and quietly extend the platform's retention window.
    """
    max_age = (
        max_age_hours if max_age_hours is not None else settings.VOXTELL_VOLUME_MAX_AGE_HOURS
    )
    result_ttl = (
        result_ttl_hours if result_ttl_hours is not None else settings.VOXTELL_RESULT_TTL_HOURS
    )
    return max_age <= result_ttl

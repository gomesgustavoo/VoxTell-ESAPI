"""Which job states compete for the GPU.

One assertion, and a long reason for it.

``awaiting_upload`` used to be in ``OUTSTANDING_STATES``. The outstanding cap is
``MAX_RUNNING_PER_USER + MAX_QUEUED_PER_USER`` == 6, and an abandoned upload is
only reaped after ``VOXTELL_UPLOAD_TTL_MINUTES`` == 120. So six failed uploads
inside two hours made every subsequent ``POST /v1/jobs`` return 429 — advertising
``Retry-After: 30``, wrong by two orders of magnitude — and, because the
``UsageEvent`` was written at create rather than at submit, those six had already
spent six of the user's 200 monthly units without a single byte being segmented.

That is a lockout produced entirely by counting the wrong thing: a job waiting for
bytes is not competing for the GPU. Open uploads are a *storage* concern and are
bounded separately by ``VOXTELL_MAX_AWAITING_UPLOAD_PER_USER``.
"""

from __future__ import annotations

from api.config import settings
from api.models import JOB_STATES
from api.quota import OUTSTANDING_STATES


def test_awaiting_upload_does_not_consume_a_gpu_slot():
    """Do not "fix" this by adding it back — read the module docstring first."""
    assert "awaiting_upload" not in OUTSTANDING_STATES


def test_outstanding_means_exactly_queued_plus_running():
    assert set(OUTSTANDING_STATES) == {"queued", "running"}


def test_every_outstanding_state_is_a_real_job_state():
    """Catches a typo that would silently make the cap unenforceable."""
    for state in OUTSTANDING_STATES:
        assert state in JOB_STATES


def test_no_terminal_state_is_counted_as_outstanding():
    for state in ("done", "failed", "cancelled", "expired"):
        assert state not in OUTSTANDING_STATES


def test_storage_and_gpu_limits_are_separate_knobs():
    """The structural fix, not just the value change.

    One counter used to serve both concerns, which is what let a run of failed
    uploads deny GPU access. They are now two settings and must stay that way.
    """
    gpu_cap = settings.VOXTELL_MAX_RUNNING_PER_USER + settings.VOXTELL_MAX_QUEUED_PER_USER
    assert gpu_cap == 6
    assert settings.VOXTELL_MAX_AWAITING_UPLOAD_PER_USER == 3
    assert settings.VOXTELL_MAX_AWAITING_UPLOAD_PER_USER < gpu_cap

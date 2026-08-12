"""How long an uploaded volume lives.

Pure arithmetic over ``api.retention``, because this is the code that decides how
long patient voxels sit on disk and it should be provable without a database.

The test that matters most is
:func:`test_volume_never_outlives_the_results_derived_from_it`. The whole privacy
argument for keeping volumes at all rests on that inequality, and an inequality
between two config values is exactly the kind of claim that quietly stops being
true when someone raises one of them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.config import settings
from api.retention import is_expired, max_age_within_result_ttl, next_expiry

CREATED = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)


def test_idle_window_binds_while_the_volume_is_young():
    now = CREATED + timedelta(minutes=5)
    expiry = next_expiry(CREATED, now, idle_minutes=120, max_age_hours=8)
    assert expiry == now + timedelta(minutes=120)


def test_the_hard_ceiling_binds_near_the_end_of_the_window():
    """Late in a volume's life the idle TTL would push past the ceiling."""
    now = CREATED + timedelta(hours=7, minutes=30)
    expiry = next_expiry(CREATED, now, idle_minutes=120, max_age_hours=8)
    assert expiry == CREATED + timedelta(hours=8)


def test_expiry_is_never_later_than_the_ceiling_however_often_it_slides():
    """The property that makes the ceiling meaningful.

    A volume in constant use gets its expiry recomputed on every job and every
    poll. If sliding could ever exceed the ceiling, an active session would keep a
    patient's CT resident indefinitely — which is the failure this bounds.
    """
    ceiling = CREATED + timedelta(hours=8)
    now = CREATED
    for _ in range(200):  # ~16 h of five-minute touches
        now += timedelta(minutes=5)
        assert next_expiry(CREATED, now, idle_minutes=120, max_age_hours=8) <= ceiling


def test_volume_never_outlives_the_results_derived_from_it():
    """The load-bearing claim of the privacy argument.

    Contours are sparse and derived; a full head CT is re-identifiable. So the
    defensible position is that enabling reuse does not extend the platform's
    *maximum* patient-data retention — the input now occupies a slice of a window
    the results already own. That holds only while the volume ceiling stays inside
    the result TTL.

    If this fails, do not relax the test. Either lower
    VOXTELL_VOLUME_MAX_AGE_HOURS or make the case for a longer window explicitly.
    """
    assert max_age_within_result_ttl()
    assert settings.VOXTELL_VOLUME_MAX_AGE_HOURS <= settings.VOXTELL_RESULT_TTL_HOURS


def test_the_shipped_defaults_are_the_ones_documented():
    """PROTOCOL.md and the privacy argument both quote these numbers."""
    assert settings.VOXTELL_VOLUME_TTL_MINUTES == 120
    assert settings.VOXTELL_VOLUME_MAX_AGE_HOURS == 8
    assert settings.VOXTELL_MAX_VOLUMES_PER_USER == 3
    # Matches VOXTELL_UPLOAD_TTL_MINUTES so an operator holds one number.
    assert settings.VOXTELL_VOLUME_TTL_MINUTES == settings.VOXTELL_UPLOAD_TTL_MINUTES


def test_volumes_ship_disabled():
    """The rollout kill switch defaults off.

    Deliberate: during the API-before-worker deploy window an old worker still
    deletes its job's volume unconditionally, which would destroy a shared object
    after the first job. The flag makes that sequence unreachable.
    """
    assert settings.VOXTELL_VOLUMES_ENABLED is False


def test_is_expired_is_inclusive_at_the_boundary():
    now = CREATED + timedelta(hours=1)
    assert is_expired(now, now)
    assert is_expired(now - timedelta(seconds=1), now)
    assert not is_expired(now + timedelta(seconds=1), now)

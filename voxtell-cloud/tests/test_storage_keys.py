"""The object-key layout, pinned as plain string assertions.

This is the highest-value test in the volume feature, and it is worth saying why
so nobody deletes it as trivial.

Four separate code paths purge storage with
``delete_prefix(job_prefix(user_id, job_id))``: deleting a job, cancelling a job,
the ``/submit`` size-mismatch bail-out, and the sweeper's result expiry. A shared
volume is read by *many* jobs, so if it ever lived under one job's prefix, any of
those four would silently destroy the input the other jobs still need — and the
symptom would appear later, as a job failing on a missing object, far from the
delete that caused it.

Keeping shared volumes outside ``jobs/`` makes all four automatically correct.
That safety property is pure string arithmetic, so it can be pinned here with no
database, no S3 and no fixtures — which is exactly the kind of guard that survives.
"""

from __future__ import annotations

import uuid

from api import storage

USER = uuid.UUID("11111111-2222-3333-4444-555555555555")
JOB = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
SHA = "9f" * 32


def test_shared_volume_is_not_under_the_jobs_subtree():
    key = storage.shared_volume_key(USER, SHA)
    assert f"u/{USER}/jobs/" not in key
    assert "/jobs/" not in key


def test_no_job_prefix_is_a_prefix_of_a_shared_volume_key():
    """The property the four purge sites depend on.

    Checked against several job ids rather than one, because the bug this guards
    against would be a key template that happened to collide for some ids.
    """
    key = storage.shared_volume_key(USER, SHA)
    for _ in range(20):
        prefix = storage.job_prefix(USER, uuid.uuid4())
        assert not key.startswith(prefix)


def test_shared_volume_stays_inside_the_user_subtree():
    """Erasure is one ``delete_prefix("u/{user}/")`` call and must stay that way.

    This is also what makes per-user dedup the safe choice: a globally shared
    object could not live under any one user's prefix, so "delete everything for
    this user" would stop being complete.
    """
    key = storage.shared_volume_key(USER, SHA)
    assert key.startswith(f"u/{USER}/")


def test_shared_volume_key_is_content_addressed():
    """Same content, same key — which is what makes completing an upload twice a
    no-op rather than a duplicate object."""
    assert storage.shared_volume_key(USER, SHA) == storage.shared_volume_key(USER, SHA)
    other = storage.shared_volume_key(USER, "ab" * 32)
    assert other != storage.shared_volume_key(USER, SHA)


def test_different_users_never_share_a_key():
    a = storage.shared_volume_key(uuid.uuid4(), SHA)
    b = storage.shared_volume_key(uuid.uuid4(), SHA)
    assert a != b


def test_legacy_job_volume_is_still_under_its_job_prefix():
    """The legacy path is unchanged, and its eager purge still works."""
    key = storage.volume_key(USER, JOB)
    assert key.startswith(storage.job_prefix(USER, JOB))


def test_result_and_mask_stay_under_the_job_prefix():
    """Results are per-job and SHOULD be purged with the job — the opposite of
    volumes. Pinned so the two lifetimes do not get conflated later."""
    prefix = storage.job_prefix(USER, JOB)
    assert storage.result_key(USER, JOB).startswith(prefix)
    assert storage.mask_key(USER, JOB).startswith(prefix)

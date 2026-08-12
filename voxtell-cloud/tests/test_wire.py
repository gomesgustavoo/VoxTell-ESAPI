"""Wire format: part arithmetic and the result envelope.

The part maths is worth testing because an off-by-one produces a job that
uploads successfully and then fails at `submit` with a size mismatch — a
confusing failure mode for a client author to debug.
"""

from __future__ import annotations

import pytest

from voxtell_cloud.wire import (
    MIN_PART_SIZE,
    PART_SIZE,
    RESULT_SCHEMA_VERSION,
    part_count,
    result_envelope,
)


def test_part_size_satisfies_s3_and_cloudflare():
    # S3 rejects a non-final part below 5 MiB; Cloudflare rejects bodies over
    # 100 MB. PART_SIZE has to sit between the two.
    assert MIN_PART_SIZE <= PART_SIZE < 100 * 1000 * 1000


@pytest.mark.parametrize(
    "total,expected",
    [
        (1, 1),
        (PART_SIZE - 1, 1),
        (PART_SIZE, 1),
        (PART_SIZE + 1, 2),
        (2 * PART_SIZE, 2),
        (2 * PART_SIZE + 1, 3),
    ],
)
def test_part_count_boundaries(total, expected):
    assert part_count(total) == expected


def test_part_count_rejects_empty():
    with pytest.raises(ValueError):
        part_count(0)


def test_parts_tile_the_blob_exactly():
    """Slicing by PART_SIZE must reproduce the blob byte for byte."""
    blob = bytes(range(256)) * 1000  # 256 000 bytes
    n = part_count(len(blob))
    parts = [blob[(i - 1) * PART_SIZE : i * PART_SIZE] for i in range(1, n + 1)]
    assert b"".join(parts) == blob
    # Every part except the last is full-size — the rule S3 enforces.
    assert all(len(p) == PART_SIZE for p in parts[:-1])
    assert 0 < len(parts[-1]) <= PART_SIZE


def test_result_envelope_shape():
    env = result_envelope(
        job_id="abc",
        model="voxtell_v1.1",
        prompts=("liver", "spleen"),
        results=[{"prompt": "liver", "voxel_count": 1, "contours": []}],
    )
    assert env["schema"] == RESULT_SCHEMA_VERSION
    assert env["job_id"] == "abc"
    assert env["model"] == "voxtell_v1.1"
    # Prompts are normalised to a list so the JSON is stable regardless of what
    # the caller passed (tuple, generator, ...).
    assert env["prompts"] == ["liver", "spleen"]
    assert env["results"][0]["prompt"] == "liver"

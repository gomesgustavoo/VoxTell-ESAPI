"""The content hash, end to end through the real decoder.

``content_sha256`` is a volume's dedup identity. If a stored digest did not match
the bytes it names, a *later* upload of a different series could match it and be
served this volume — and a planner would receive contours belonging to another
patient's CT, on a study that looks correct on screen. That makes this a
patient-safety check rather than an integrity nicety, which is why it is verified
at all rather than trusted.

It is verified in the worker, where ``decode_volume`` already has the whole
uncompressed buffer in memory. Verifying in the API would mean pulling tens of
megabytes back out of S3 and through a process whose defining property is that the
volume never passes through it.

The definition being pinned: **sha256 of the uncompressed int16-LE (Z,Y,X) stream**,
not of the gzip output. Gzip is not canonical — the C# client comments on having
*chosen* ``CompressionLevel.Fastest``, so that knob is live, and a framework upgrade
that changed the deflate output would silently invalidate every held volume.
"""

from __future__ import annotations

import gzip
import hashlib

import numpy as np
import pytest

from voxtell_cloud.geometry import decode_volume

X, Y, Z = 16, 12, 5


def voxels() -> np.ndarray:
    rng = np.random.default_rng(20260805)
    return rng.integers(-1000, 3000, size=(Z, Y, X), dtype=np.int16)


def raw_bytes(arr: np.ndarray) -> bytes:
    """Exactly what the C# encoder writes: int16-LE, C-order (Z, Y, X)."""
    return arr.astype("<i2").tobytes(order="C")


def blob(arr: np.ndarray) -> bytes:
    return gzip.compress(raw_bytes(arr))


def digest(arr: np.ndarray) -> str:
    return hashlib.sha256(raw_bytes(arr)).hexdigest()


def test_the_correct_hash_is_accepted():
    arr = voxels()
    out = decode_volume(blob(arr), X, Y, Z, expect_content_sha256=digest(arr))
    assert out.shape == (Z, Y, X)
    np.testing.assert_array_equal(out.astype(np.int16), arr)


def test_a_wrong_hash_is_rejected():
    arr = voxels()
    with pytest.raises(ValueError, match="content hash"):
        decode_volume(blob(arr), X, Y, Z, expect_content_sha256="ab" * 32)


def test_hash_of_the_gzip_output_is_rejected():
    """The most likely way to get this wrong, so it is pinned as a failure.

    A client that hashed the compressed bytes would appear to work — every upload
    would verify against its own digest — right up until the compression level
    changed and every cached volume silently missed.
    """
    arr = voxels()
    compressed_digest = hashlib.sha256(blob(arr)).hexdigest()
    with pytest.raises(ValueError, match="content hash"):
        decode_volume(blob(arr), X, Y, Z, expect_content_sha256=compressed_digest)


def test_verification_is_case_insensitive():
    """The schema lower-cases on the way in, but the worker reads the stored JSONB
    directly, so it must tolerate either spelling on its own."""
    arr = voxels()
    out = decode_volume(blob(arr), X, Y, Z, expect_content_sha256=digest(arr).upper())
    assert out.shape == (Z, Y, X)


def test_omitting_the_hash_skips_verification():
    """Legacy inline-upload jobs have no stored digest and must still decode."""
    arr = voxels()
    out = decode_volume(blob(arr), X, Y, Z)
    assert out.shape == (Z, Y, X)
    out = decode_volume(blob(arr), X, Y, Z, expect_content_sha256=None)
    assert out.shape == (Z, Y, X)


def test_a_single_flipped_voxel_is_caught():
    """The size check cannot catch this; only the hash can."""
    arr = voxels()
    tampered = arr.copy()
    tampered[Z // 2, Y // 2, X // 2] += 1
    with pytest.raises(ValueError, match="content hash"):
        decode_volume(blob(tampered), X, Y, Z, expect_content_sha256=digest(arr))


def test_the_length_check_still_runs_first():
    """A truncated upload should report truncation, not a hash mismatch.

    Ordering matters for diagnosis: "3200 bytes, expected 1920" tells an operator
    the upload was cut, while a hash mismatch would send them looking for
    corruption.
    """
    arr = voxels()
    with pytest.raises(ValueError, match="bytes, expected"):
        decode_volume(blob(arr), X, Y, Z + 3, expect_content_sha256=digest(arr))


def test_rescale_does_not_change_the_verified_bytes():
    """The hash covers stored values; the HU rescale is applied after."""
    arr = voxels()
    out = decode_volume(
        blob(arr), X, Y, Z,
        scaling_slope=1.0, scaling_intercept=-1024.0,
        expect_content_sha256=digest(arr),
    )
    np.testing.assert_allclose(out, arr.astype(np.float32) - 1024.0)

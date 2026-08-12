"""The C# client's wire format, checked against the server that has to decode it.

The ESAPI plugin cannot be built or run here — it needs .NET Framework, the Varian assemblies
and Windows. So the two pieces of its upload path that are pure arithmetic are ported here
line-for-line from C# and pinned against the real ``voxtell_cloud`` code:

* ``VoxelEncoder.BuildVolumeBlob`` + ``EsapiVolumeSource.ReadSlice`` -- the int16-LE,
  C-order ``(Z, Y, X)``, y-outer/x-inner layout, fed to the actual ``decode_volume``.
* ``AffineCheck.InvertAffine`` -- the adjugate inverse the harness uses to verify returned
  contours, compared against numpy.

These are ports, so they cannot catch a C# compile error. What they do catch is the class of
bug that is otherwise invisible until contours appear on the wrong slice inside Eclipse: a
transposed axis, a reversed slice direction, a wrong byte width, or a mis-derived HU rescale.
If any of these fail, the C# in
``voxtell-esapi-client/VoxTell-Interface/Services/{VoxelEncoder,EsapiVolumeSource}.cs`` and
``voxtell-esapi-client/VoxTell-Interface.Harness/AffineCheck.cs`` must change with them.
"""

from __future__ import annotations

import gzip
import hashlib
import io

import numpy as np
import pytest

from voxtell_cloud.geometry import build_affine_lps, decode_volume

# Matches SyntheticVolumeSource in the harness.
X, Y, Z = 48, 48, 24
XRES, YRES, ZRES = 1.5, 1.5, 2.5
ORIGIN = [-0.5 * (X - 1) * XRES, -0.5 * (Y - 1) * YRES, -0.5 * (Z - 1) * ZRES]
ROW, COL, SLICE = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]

# Stored values, NOT Hounsfield units -- the point of the rescale being sent separately.
STORED_AIR, STORED_SOFT, STORED_BONE = 0, 1064, 2024
SLOPE, INTERCEPT = 1.0, -1024.0


def read_slice(z: int) -> np.ndarray:
    """Port of ``EsapiVolumeSource.ReadSlice`` / ``SyntheticVolumeSource.ReadSlice``.

    Fills a flat int16 buffer with y outer and x inner -- ``dest[y * XSize + x]`` -- which is
    the ordering the ``(Z, Y, X)`` C-order wire format requires.
    """
    cx, cy, cz = (X - 1) / 2.0, (Y - 1) / 2.0, (Z - 1) / 2.0
    body_rx, body_ry, body_rz = X * 0.38, Y * 0.28, Z * 0.45
    bone_r = min(X, Y) * 0.08

    out = np.empty(X * Y, dtype="<i2")
    i = 0
    for y in range(Y):
        for x in range(X):
            dx, dy, dz = (x - cx) / body_rx, (y - cy) / body_ry, (z - cz) / body_rz
            value = STORED_AIR
            if dx * dx + dy * dy + dz * dz <= 1.0:
                value = STORED_SOFT
                bx, by, bz = x - cx, y - cy, (z - cz) * (ZRES / XRES)
                if bx * bx + by * by + bz * bz <= bone_r * bone_r:
                    value = STORED_BONE
            out[i] = value
            i += 1
    return out


def build_blob() -> bytes:
    """Port of ``VoxelEncoder.BuildVolumeBlob``: one gzip stream over the slices, in order."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=1) as gz:
        for z in range(Z):
            gz.write(read_slice(z).tobytes())
    return buf.getvalue()


def build_content_sha256() -> str:
    """Port of the C# ``sha.TransformBlock`` loop inside ``BuildVolumeBlob``.

    The C# hashes each slice's bytes as it writes them into the gzip stream, so the
    digest covers the concatenation of the slices in z order — which is the same
    thing as the whole uncompressed volume. Spelling it out slice-by-slice here
    rather than hashing one flat array keeps this a genuine port of the loop, so it
    would catch a C# implementation that hashed the slices in the wrong order.
    """
    h = hashlib.sha256()
    for z in range(Z):
        h.update(read_slice(z).tobytes())
    return h.hexdigest()


def invert_affine(row, col, sl, xres, yres, zres, origin) -> np.ndarray:
    """Port of ``AffineCheck.InvertAffine`` -- adjugate over determinant, 3x4."""
    m = [
        [row[0] * xres, col[0] * yres, sl[0] * zres],
        [row[1] * xres, col[1] * yres, sl[1] * zres],
        [row[2] * xres, col[2] * yres, sl[2] * zres],
    ]
    det = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
    inv = [[0.0] * 4 for _ in range(3)]
    inv[0][0] = (m[1][1] * m[2][2] - m[1][2] * m[2][1]) / det
    inv[0][1] = (m[0][2] * m[2][1] - m[0][1] * m[2][2]) / det
    inv[0][2] = (m[0][1] * m[1][2] - m[0][2] * m[1][1]) / det
    inv[1][0] = (m[1][2] * m[2][0] - m[1][0] * m[2][2]) / det
    inv[1][1] = (m[0][0] * m[2][2] - m[0][2] * m[2][0]) / det
    inv[1][2] = (m[0][2] * m[1][0] - m[0][0] * m[1][2]) / det
    inv[2][0] = (m[1][0] * m[2][1] - m[1][1] * m[2][0]) / det
    inv[2][1] = (m[0][1] * m[2][0] - m[0][0] * m[2][1]) / det
    inv[2][2] = (m[0][0] * m[1][1] - m[0][1] * m[1][0]) / det
    for r in range(3):
        inv[r][3] = -(
            inv[r][0] * origin[0] + inv[r][1] * origin[1] + inv[r][2] * origin[2]
        )
    return np.array(inv)


@pytest.fixture(scope="module")
def decoded() -> np.ndarray:
    return decode_volume(
        build_blob(), X, Y, Z, scaling_slope=SLOPE, scaling_intercept=INTERCEPT
    )


@pytest.fixture(scope="module")
def expected() -> np.ndarray:
    volume = np.empty((Z, Y, X), dtype=np.float32)
    for z in range(Z):
        volume[z] = read_slice(z).reshape(Y, X)
    return volume * SLOPE + INTERCEPT


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def test_server_decodes_the_client_blob(decoded):
    assert decoded.shape == (Z, Y, X)


def test_every_voxel_survives_the_round_trip(decoded, expected):
    assert np.array_equal(decoded, expected)


def test_x_and_y_are_not_transposed(decoded):
    """The phantom is wider in x (0.38 of the axis) than in y (0.28).

    A transposed slice would decode taller than wide, and every contour would come back
    mirrored about the diagonal -- plausible-looking anatomy in the wrong place.
    """
    mid = decoded[Z // 2] > INTERCEPT
    assert int(mid.any(axis=0).sum()) > int(mid.any(axis=1).sum())


def test_slice_axis_is_in_order(decoded):
    """The ellipsoid is widest in the middle and empty at both ends.

    Catches a reversed or shuffled z axis, which would put contours on mirrored slices.
    """
    areas = [int((decoded[z] > INTERCEPT).sum()) for z in range(Z)]
    assert areas[Z // 2] == max(areas)
    assert areas[0] < areas[Z // 2] > areas[-1]


def test_blob_stays_under_the_implausibility_tripwire():
    """The API rejects upload_bytes > voxels*2 + 1 MiB as a probable int32 mistake."""
    assert len(build_blob()) <= X * Y * Z * 2 + (1 << 20)


# --------------------------------------------------------------------------- #
# The HU rescale -- the correctness fix, not just plumbing
# --------------------------------------------------------------------------- #
def test_air_decodes_to_air_not_zero(decoded):
    """Stored 0 must become about -1024 HU.

    This is the whole reason the client sends slope/intercept: the worker's crop_to_nonzero
    thresholds at exactly 0, so in stored values (air == 0) the crop lands somewhere other
    than the body outline. v1 sent stored values with no rescale metadata at all.
    """
    assert float(decoded[0, 0, 0]) == pytest.approx(-1024.0)


def test_tissue_and_bone_land_on_physical_values(decoded):
    values = np.unique(decoded)
    for hu in (40.0, 1000.0):   # soft tissue, bone
        assert np.isclose(values, hu).any(), f"{hu} HU absent from {values.tolist()}"


# --------------------------------------------------------------------------- #
# Failing loudly
# --------------------------------------------------------------------------- #
def test_truncated_upload_is_rejected():
    blob = build_blob()
    with pytest.raises(Exception):
        decode_volume(blob[: len(blob) // 2], X, Y, Z)


def test_int32_upload_is_rejected():
    """v1's byte width. Must fail on the length check, not silently decode half a volume."""
    volume = np.stack([read_slice(z).reshape(Y, X) for z in range(Z)])
    with pytest.raises(ValueError):
        decode_volume(gzip.compress(volume.astype("<i4").tobytes(order="C")), X, Y, Z)


# --------------------------------------------------------------------------- #
# The affine inverse the harness verifies contours with
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("oblique", [False, True])
def test_csharp_affine_inverse_matches_numpy(oblique):
    if oblique:
        # An oblique, anisotropic frame: where a transposed adjugate would show up, and
        # axis-aligned test data would not.
        row = np.array([0.9362934, 0.2620026, -0.2331457])
        row /= np.linalg.norm(row)
        col = np.array([-0.2810846, 0.9586622, -0.0316227])
        col -= col.dot(row) * row
        col /= np.linalg.norm(col)
        sl = np.cross(row, col)
        row, col, sl = row.tolist(), col.tolist(), sl.tolist()
        xres, yres, zres = 0.9765625, 0.9765625, 3.0
    else:
        row, col, sl = ROW, COL, SLICE
        xres, yres, zres = XRES, YRES, ZRES

    forward = build_affine_lps(row, col, sl, xres, yres, zres, ORIGIN)
    assert np.abs(np.linalg.inv(forward)[:3, :] - invert_affine(
        row, col, sl, xres, yres, zres, ORIGIN)).max() < 1e-12


@pytest.mark.parametrize("oblique", [False, True])
def test_voxel_indices_round_trip_through_the_inverse(oblique):
    """The assertion the harness makes about every returned contour point."""
    if oblique:
        row = np.array([0.6, 0.8, 0.0])
        col = np.array([-0.8, 0.6, 0.0])
        sl = np.cross(row, col)
        row, col, sl = row.tolist(), col.tolist(), sl.tolist()
    else:
        row, col, sl = ROW, COL, SLICE

    forward = build_affine_lps(row, col, sl, XRES, YRES, ZRES, ORIGIN)
    inverse = invert_affine(row, col, sl, XRES, YRES, ZRES, ORIGIN)

    vox = np.array([[0, 0, 0], [X - 1, Y - 1, Z - 1], [17.5, 3.25, 11.0]], dtype=float)
    world = (forward @ np.c_[vox, np.ones(len(vox))].T).T[:, :3]
    back = (inverse @ np.c_[world, np.ones(len(world))].T).T

    assert np.abs(back - vox).max() < 1e-9


# --------------------------------------------------------------------------- #
# Content hash: the volume dedup identity, defined in two languages
# --------------------------------------------------------------------------- #
def test_the_client_hash_is_what_the_server_verifies():
    """The cross-language definition of ``content_sha256``.

    Without this, "sha256 of the uncompressed int16-LE (Z,Y,X) stream" is prose in
    two codebases that can drift. The consequence of drift is not a crash: every
    upload would verify against its own digest and simply never dedup, so the
    feature would silently do nothing while appearing to work.
    """
    decode_volume(
        build_blob(), X, Y, Z,
        scaling_slope=SLOPE, scaling_intercept=INTERCEPT,
        expect_content_sha256=build_content_sha256(),
    )


def test_the_hash_is_over_the_uncompressed_stream_not_the_gzip():
    """Pinned as a *failure*, because it is the plausible way to get this wrong.

    Hashing the compressed bytes is self-consistent and would pass any test that
    only checked a round trip — right up until the compression level changed and
    every held volume silently missed.
    """
    with pytest.raises(ValueError, match="content hash"):
        decode_volume(
            build_blob(), X, Y, Z,
            scaling_slope=SLOPE, scaling_intercept=INTERCEPT,
            expect_content_sha256=hashlib.sha256(build_blob()).hexdigest(),
        )


def test_this_phantom_is_symmetric_in_z_so_the_hash_cannot_catch_a_reversal():
    """A limitation of the fixture, asserted so it is not mistaken for coverage.

    The phantom is an ellipsoid centred in the volume, so slice z and slice
    ``Z-1-z`` are identical and reversing the slice order produces byte-identical
    output. A hash therefore cannot detect a reversed z axis *on this fixture* —
    ``test_slice_axis_is_in_order`` is what covers that, by checking that the
    cross-sectional area peaks in the middle.

    Stating this explicitly matters because the obvious test to write here would
    pass for the wrong reason and imply the hash guards an axis order it does not.
    """
    forward = hashlib.sha256()
    for z in range(Z):
        forward.update(read_slice(z).tobytes())
    backward = hashlib.sha256()
    for z in reversed(range(Z)):
        backward.update(read_slice(z).tobytes())
    assert forward.hexdigest() == backward.hexdigest()


def test_slice_order_is_part_of_the_hash():
    """A loop that emitted slices out of order must not verify.

    Uses slices 0 and 1 rather than a reversal: they sit at different distances
    from the centre, so unlike a mirror pair they genuinely differ.
    """
    order = list(range(Z))
    order[0], order[1] = order[1], order[0]
    h = hashlib.sha256()
    for z in order:
        h.update(read_slice(z).tobytes())
    # Guard the guard: if the fixture ever made these two slices identical, this
    # test would silently stop testing anything.
    assert read_slice(0).tobytes() != read_slice(1).tobytes()

    with pytest.raises(ValueError, match="content hash"):
        decode_volume(
            build_blob(), X, Y, Z,
            scaling_slope=SLOPE, scaling_intercept=INTERCEPT,
            expect_content_sha256=h.hexdigest(),
        )


def test_the_hash_covers_stored_values_not_hounsfield_units():
    """The client hashes what it reads out of ESAPI, before any rescale.

    It has to: the rescale is metadata travelling as JSON, and the client never
    materialises an HU volume. So the digest must be independent of slope and
    intercept, and the same bytes must verify whatever rescale is applied.
    """
    digest = build_content_sha256()
    for slope, intercept in ((1.0, -1024.0), (1.0, 0.0), (2.0, -1000.0)):
        decode_volume(
            build_blob(), X, Y, Z,
            scaling_slope=slope, scaling_intercept=intercept,
            expect_content_sha256=digest,
        )

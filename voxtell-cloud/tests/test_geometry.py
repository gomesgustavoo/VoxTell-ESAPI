"""Geometry: LPS affine construction, HU decode, and the RAS round trip.

These are the parts where a silent sign error produces a plausible-looking but
mirrored segmentation, so they are checked against an oblique, anisotropic,
non-axis-aligned geometry rather than a friendly identity one.
"""

from __future__ import annotations

import gzip
import math

import numpy as np
import pytest

from voxtell_cloud.geometry import (
    affine_lps_to_ras,
    build_affine_lps,
    decode_volume,
    voxels_to_lps,
    write_nifti_ras,
)

# A head-first-supine-ish geometry with a 15 degree in-plane rotation and
# anisotropic spacing — nothing here is symmetric enough to hide a transpose.
ANGLE = math.radians(15)
ROW = [math.cos(ANGLE), math.sin(ANGLE), 0.0]
COL = [-math.sin(ANGLE), math.cos(ANGLE), 0.0]
SLICE = [0.0, 0.0, 1.0]
ORIGIN = [-243.7, -211.5, -88.25]
RES = (0.9765625, 0.9765625, 2.5)


def oblique_affine() -> np.ndarray:
    return build_affine_lps(
        row_direction=ROW, col_direction=COL, slice_direction=SLICE,
        x_res=RES[0], y_res=RES[1], z_res=RES[2], origin=ORIGIN,
    )


def test_affine_columns_are_direction_times_spacing():
    aff = oblique_affine()
    np.testing.assert_allclose(aff[:3, 0], np.array(ROW) * RES[0])
    np.testing.assert_allclose(aff[:3, 1], np.array(COL) * RES[1])
    np.testing.assert_allclose(aff[:3, 2], np.array(SLICE) * RES[2])
    np.testing.assert_allclose(aff[:3, 3], ORIGIN)
    # Voxel (0,0,0) must land exactly on the DICOM origin.
    np.testing.assert_allclose(voxels_to_lps(np.zeros((1, 3)), aff)[0], ORIGIN)


def test_voxel_lps_round_trip():
    aff = oblique_affine()
    vox = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [255, 187, 63]], float)
    lps = voxels_to_lps(vox, aff)

    homogeneous = np.c_[lps, np.ones(len(lps))]
    back = (np.linalg.inv(aff) @ homogeneous.T).T[:, :3]
    np.testing.assert_allclose(back, vox, atol=1e-9)


def test_one_voxel_step_matches_spacing():
    """A single index step must move exactly one voxel's worth of millimetres."""
    aff = oblique_affine()
    origin = voxels_to_lps(np.zeros((1, 3)), aff)[0]
    for axis, expected in enumerate(RES):
        vox = np.zeros((1, 3))
        vox[0, axis] = 1
        step = voxels_to_lps(vox, aff)[0] - origin
        assert np.linalg.norm(step) == pytest.approx(expected)


def test_lps_to_ras_flips_only_the_first_two_axes():
    aff = oblique_affine()
    ras = affine_lps_to_ras(aff)
    np.testing.assert_allclose(ras[0, :], -aff[0, :])
    np.testing.assert_allclose(ras[1, :], -aff[1, :])
    np.testing.assert_allclose(ras[2, :], aff[2, :])
    # Applying it twice is the identity, which is what makes it its own inverse.
    np.testing.assert_allclose(affine_lps_to_ras(ras), aff)


# --------------------------------------------------------------------------- #
# Volume decoding
# --------------------------------------------------------------------------- #
def make_blob(volume_zyx: np.ndarray) -> bytes:
    return gzip.compress(volume_zyx.astype("<i2").tobytes(order="C"))


def test_decode_volume_preserves_shape_and_order():
    # Distinct value per voxel so any transpose or endianness slip shows up.
    z, y, x = 4, 3, 5
    volume = np.arange(z * y * x, dtype=np.int16).reshape(z, y, x)
    out = decode_volume(make_blob(volume), x_size=x, y_size=y, z_size=z)
    assert out.shape == (z, y, x)
    np.testing.assert_array_equal(out, volume.astype(np.float32))


def test_decode_volume_applies_hu_rescale():
    """Stored values -> HU. Getting this wrong shifts crop_to_nonzero's box."""
    volume = np.array([[[0, 1000, 2000]]], dtype=np.int16)
    out = decode_volume(
        make_blob(volume), x_size=3, y_size=1, z_size=1,
        scaling_slope=1.0, scaling_intercept=-1024.0,
    )
    np.testing.assert_allclose(out, [[[-1024.0, -24.0, 976.0]]])


def test_decode_volume_rejects_a_truncated_upload():
    volume = np.zeros((2, 2, 2), dtype=np.int16)
    with pytest.raises(ValueError, match="expected"):
        decode_volume(make_blob(volume), x_size=2, y_size=2, z_size=3)


def test_nifti_round_trip_recovers_the_lps_affine(tmp_path):
    """Write RAS, read back, and confirm the LPS geometry survived intact."""
    nib = pytest.importorskip("nibabel")

    aff = oblique_affine()
    volume = np.random.default_rng(0).integers(-1000, 2000, (6, 5, 4)).astype(np.float32)
    path = str(tmp_path / "v.nii.gz")
    write_nifti_ras(volume, aff, path)

    img = nib.load(path)
    # NIfTI holds (X, Y, Z); we handed it (Z, Y, X).
    assert img.shape == (4, 5, 6)
    np.testing.assert_allclose(np.asanyarray(img.dataobj), volume.transpose(2, 1, 0))
    # The stored affine is the RAS one; flipping it back gives us LPS again.
    np.testing.assert_allclose(affine_lps_to_ras(img.affine), aff, atol=1e-6)

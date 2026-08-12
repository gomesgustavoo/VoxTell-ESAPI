"""Contour extraction: the masks -> ESAPI hand-off.

Checked against a synthetic sphere so the expected geometry is known exactly:
the traced boundary must sit on the sphere's surface, in LPS millimetres, at the
right z-slices, with rings and multi-lobed slices handled correctly.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from voxtell_cloud.contours import DEFAULT_MIN_POINTS, PromptResult, extract_contours
from voxtell_cloud.geometry import build_affine_lps

SPACING = (0.8, 0.8, 2.0)
ORIGIN = [-100.0, -80.0, -60.0]


def affine() -> np.ndarray:
    return build_affine_lps(
        row_direction=[1, 0, 0], col_direction=[0, 1, 0], slice_direction=[0, 0, 1],
        x_res=SPACING[0], y_res=SPACING[1], z_res=SPACING[2], origin=ORIGIN,
    )


def sphere(shape=(48, 48, 24), centre=(24, 24, 12), radius=10.0) -> np.ndarray:
    """Binary (X, Y, Z) ball, in voxel units."""
    gx, gy, gz = np.ogrid[: shape[0], : shape[1], : shape[2]]
    d2 = (gx - centre[0]) ** 2 + (gy - centre[1]) ** 2 + (gz - centre[2]) ** 2
    return (d2 <= radius**2).astype(np.uint8)


def test_empty_mask_yields_no_contours():
    assert extract_contours(np.zeros((8, 8, 4), np.uint8), affine()) == []


def test_sphere_contours_cover_the_occupied_slices():
    mask = sphere()
    contours = extract_contours(mask, affine())

    occupied = {z for z in range(mask.shape[2]) if mask[:, :, z].any()}
    traced = {c["z_index"] for c in contours}
    # Slices with only a handful of voxels fall under the fragment filter, so
    # traced is a subset — but it must cover the bulk of the sphere.
    assert traced <= occupied
    assert len(traced) >= len(occupied) - 2


def test_contour_points_lie_on_the_sphere_surface_in_lps():
    """The real assertion: voxel indices were projected with the right affine."""
    centre_vox = np.array([24, 24, 12], float)
    radius_vox = 10.0
    mask = sphere(centre=tuple(centre_vox.astype(int)), radius=radius_vox)
    aff = affine()

    centre_lps = np.asarray(ORIGIN) + centre_vox * np.asarray(SPACING)

    for contour in extract_contours(mask, aff):
        z = contour["z_index"]
        # Radius of the sphere's cross-section at this slice, in millimetres.
        dz_vox = z - centre_vox[2]
        r_slice_vox = math.sqrt(max(radius_vox**2 - dz_vox**2, 0.0))
        if r_slice_vox < 3:  # tiny caps: discretisation dominates
            continue
        expected_mm = r_slice_vox * SPACING[0]  # isotropic in-plane

        pts = np.asarray(contour["points_lps"])
        assert pts.shape[1] == 3
        # Every point sits on this slice's plane...
        np.testing.assert_allclose(pts[:, 2], centre_lps[2] + dz_vox * SPACING[2])
        # ...and one voxel-diagonal from the expected circle.
        radii = np.linalg.norm(pts[:, :2] - centre_lps[:2], axis=1)
        assert np.abs(radii - expected_mm).max() < 1.5 * SPACING[0]


def test_a_ring_produces_two_contours_on_the_same_slice():
    """Donut anatomy must yield outer and inner boundaries, not one blob."""
    mask = np.zeros((40, 40, 1), np.uint8)
    gx, gy = np.ogrid[:40, :40]
    d2 = (gx - 20) ** 2 + (gy - 20) ** 2
    mask[..., 0] = ((d2 <= 15**2) & (d2 >= 8**2)).astype(np.uint8)

    contours = extract_contours(mask, affine())
    assert len(contours) == 2
    assert {c["z_index"] for c in contours} == {0}


def test_tiny_fragments_are_filtered():
    """A 2x2 speck traces fewer than min_points and must be dropped."""
    mask = np.zeros((20, 20, 1), np.uint8)
    mask[5:7, 5:7, 0] = 1
    assert extract_contours(mask, affine()) == []
    # ...unless the caller explicitly lowers the bar.
    assert extract_contours(mask, affine(), min_points=3) != []


def test_min_points_default_matches_the_v1_bridge():
    # The original ESAPI bridge shipped 10; changing it silently would change
    # which structures appear in Eclipse.
    assert DEFAULT_MIN_POINTS == 10


def test_rejects_a_non_3d_mask():
    with pytest.raises(ValueError, match="3-D"):
        extract_contours(np.zeros((4, 4), np.uint8), affine())


def test_prompt_result_serialises_flat():
    r = PromptResult(prompt="liver", voxel_count=3, contours=[{"z_index": 1, "points_lps": []}])
    assert r.to_json() == {
        "prompt": "liver",
        "voxel_count": 3,
        "contours": [{"z_index": 1, "points_lps": []}],
    }

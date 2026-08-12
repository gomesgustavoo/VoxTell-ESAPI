"""Binary masks -> DICOM LPS contour polylines.

The worker hands us masks that are already back in the **original DICOM-aligned
voxel space** ``(X, Y, Z) = (columns, rows, slices)`` -- nnU-Net's
``NibabelIOWithReorient.write_seg`` undoes whatever reorientation nibabel applied
when the NIfTI was read. Everything below that point is plain numpy + skimage,
which is why it lives here (torch-free, unit-testable) rather than in the worker.

For each occupied z-slice we trace the 2-D boundaries and project the voxel
coordinates through the session's LPS affine. The output feeds ESAPI directly::

    structure.AddContourOnImagePlane(points_lps, z_index);
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from skimage.measure import find_contours

from .geometry import voxels_to_lps

log = logging.getLogger(__name__)

# Contours shorter than this are marching-squares speckle, not anatomy. Eclipse
# also rejects degenerate contours, so dropping them here avoids client-side
# errors. 10 matches the value the original ESAPI bridge shipped with.
DEFAULT_MIN_POINTS = 10


@dataclass
class PromptResult:
    """Per-prompt segmentation output, ready to serialise into the envelope."""

    prompt: str
    voxel_count: int
    contours: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "voxel_count": self.voxel_count,
            "contours": self.contours,
        }


def extract_contours(
    mask_xyz: np.ndarray,
    affine_lps: np.ndarray,
    *,
    min_points: int = DEFAULT_MIN_POINTS,
) -> list[dict[str, Any]]:
    """Trace one binary mask into a list of ``{z_index, points_lps}`` entries.

    ``mask_xyz`` is DICOM-aligned ``(X, Y, Z)``. A slice may contribute several
    entries (one per closed boundary), all sharing the same ``z_index`` -- that
    is exactly how ESAPI wants multi-lobed or ring-shaped structures.
    """
    mask = np.asarray(mask_xyz)
    if mask.ndim != 3:
        raise ValueError(f"expected a 3-D (X, Y, Z) mask, got shape {mask.shape}")

    affine = np.asarray(affine_lps, dtype=np.float64)
    out: list[dict[str, Any]] = []
    dropped = 0

    for z_index in range(mask.shape[2]):
        plane = mask[:, :, z_index]
        if not plane.any():
            continue

        # find_contours indexes (row, col); our plane is (x, y), so transpose and
        # read the returned columns back as (y, x).
        for line in find_contours(plane.T.astype(np.float64), level=0.5):
            if len(line) < min_points:
                dropped += 1
                continue
            vox = np.empty((len(line), 3), dtype=np.float64)
            vox[:, 0] = line[:, 1]  # x
            vox[:, 1] = line[:, 0]  # y
            vox[:, 2] = float(z_index)
            out.append(
                {
                    "z_index": z_index,
                    "points_lps": voxels_to_lps(vox, affine).tolist(),
                }
            )

    if dropped:
        log.debug("dropped %d contour fragment(s) below %d points", dropped, min_points)
    return out


def results_for_prompts(
    masks_xyz: np.ndarray,
    prompts: list[str],
    affine_lps: np.ndarray,
    *,
    min_points: int = DEFAULT_MIN_POINTS,
) -> list[PromptResult]:
    """Run :func:`extract_contours` over a ``(P, X, Y, Z)`` mask stack."""
    masks = np.asarray(masks_xyz)
    if masks.ndim != 4 or masks.shape[0] != len(prompts):
        raise ValueError(
            f"expected masks of shape ({len(prompts)}, X, Y, Z), got {masks.shape}"
        )

    results: list[PromptResult] = []
    for i, prompt in enumerate(prompts):
        mask = masks[i]
        contours = extract_contours(mask, affine_lps, min_points=min_points)
        voxel_count = int(np.count_nonzero(mask))
        log.info(
            "prompt %r: %d voxel(s), %d contour(s)", prompt, voxel_count, len(contours)
        )
        results.append(
            PromptResult(prompt=prompt, voxel_count=voxel_count, contours=contours)
        )
    return results

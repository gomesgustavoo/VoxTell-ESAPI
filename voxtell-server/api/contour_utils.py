"""
Convert VoxTell binary masks to DICOM-compatible LPS contour points.

Strategy
--------
`NibabelIOWithReorient.write_seg()` reverses whatever RAS reorientation nibabel
applied when loading the NIfTI, saving the mask back in the original DICOM-aligned
voxel space (X, Y, Z) = (columns, rows, slices).

We then:
  1. Load that DICOM-aligned mask with plain nibabel.
  2. For each non-empty z-slice extract 2-D contours (in voxel pixel space).
  3. Convert voxel indices to LPS patient coordinates (mm) using the LPS affine
     that was stored in the session at upload time.

The resulting LPS points can be passed directly to ESAPI:
    structure.AddContourOnImagePlane(contour_points, z_index)
"""

import base64
import gzip
import logging
import tempfile
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
from skimage.measure import find_contours

from nnunetv2.imageio.nibabel_reader_writer import NibabelIOWithReorient

from .schemas import ContourSlice, SegmentationResult

log = logging.getLogger(__name__)


def _mask_to_b64(mask_xyz: np.ndarray, original_shape: tuple) -> str:
    """
    Encode a single binary mask (X, Y, Z) back to (Z, Y, X) row-major uint8,
    gzip it, and base64-encode — matching the upload convention.
    """
    x_size, y_size, z_size = original_shape
    # Ensure correct shape
    if mask_xyz.shape != (x_size, y_size, z_size):
        raise ValueError(
            f"Mask shape {mask_xyz.shape} does not match "
            f"expected ({x_size},{y_size},{z_size})"
        )
    arr_zyx = mask_xyz.transpose(2, 1, 0).astype(np.uint8)
    raw = arr_zyx.tobytes()
    return base64.b64encode(gzip.compress(raw)).decode()


def extract_contours(
    masks: np.ndarray,
    props: Any,
    affine_lps: np.ndarray,
    original_shape: tuple,
    prompts: list[str],
    include_mask: bool = False,
) -> list[SegmentationResult]:
    """
    Convert VoxTell output masks to SegmentationResult objects with LPS contours.

    Parameters
    ----------
    masks:          shape (num_prompts, X_ras, Y_ras, Z_ras), uint8 binary
    props:          image properties from NibabelIOWithReorient.read_images()
    affine_lps:     4x4 LPS affine stored at upload time
    original_shape: (x_size, y_size, z_size) of the original DICOM volume
    prompts:        list of text prompts corresponding to mask channels
    include_mask:   if True, include the raw binary mask in the response
    """
    writer = NibabelIOWithReorient()
    results = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, prompt in enumerate(prompts):
            mask_single = masks[i]  # (X_ras, Y_ras, Z_ras) uint8

            # Save back in DICOM-aligned orientation via write_seg
            tmp_mask_path = str(Path(tmp_dir) / f"mask_{i}.nii.gz")
            writer.write_seg(mask_single, tmp_mask_path, props)

            # Load the DICOM-aligned mask (X, Y, Z) = (cols, rows, slices)
            data = nib.load(tmp_mask_path).get_fdata().astype(np.uint8)
            x_d, y_d, z_d = data.shape

            contour_slices: list[ContourSlice] = []

            slices_with_contours = 0
            for z_idx in range(z_d):
                slice_2d = data[:, :, z_idx]  # shape (x_d, y_d)
                if not np.any(slice_2d):
                    continue

                # find_contours expects (rows, cols) = (y, x), so transpose
                contour_lines = find_contours(slice_2d.T.astype(float), level=0.5)

                kept = 0
                for contour_line in contour_lines:
                    n_pts = len(contour_line)

                    # Filter: skip tiny fragments (noise artifacts)
                    if n_pts < 10:
                        continue

                    vox_coords = np.ones((n_pts, 4), dtype=np.float64)
                    vox_coords[:, 0] = contour_line[:, 1]  # x
                    vox_coords[:, 1] = contour_line[:, 0]  # y
                    vox_coords[:, 2] = float(z_idx)
                    pts_lps = (vox_coords @ affine_lps.T)[:, :3]

                    contour_slices.append(
                        ContourSlice(
                            z_index=z_idx,
                            points_lps=pts_lps.tolist(),
                        )
                    )
                    kept += 1

                if kept > 0:
                    slices_with_contours += 1
                    skipped = len(contour_lines) - kept
                    if skipped > 0:
                        log.debug(
                            "Prompt %r z=%d: kept %d contour(s), filtered %d tiny fragment(s)",
                            prompt, z_idx, kept, skipped,
                        )

            log.info(
                "Prompt %r: %d contour slice(s) across %d non-empty z-slices (of %d total)",
                prompt,
                len(contour_slices),
                slices_with_contours,
                z_d,
            )

            # Optional raw mask encoding
            mask_b64 = None
            if include_mask:
                try:
                    mask_b64 = _mask_to_b64(data, original_shape)
                except ValueError:
                    pass

            results.append(
                SegmentationResult(
                    prompt=prompt,
                    contours=contour_slices,
                    mask_b64=mask_b64,
                )
            )

    return results

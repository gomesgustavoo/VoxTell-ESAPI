"""
Convert raw DICOM voxel data (from Varian ESAPI) to a NIfTI file in RAS orientation.

Coordinate convention
---------------------
Varian Eclipse uses the DICOM patient coordinate system (LPS):
  +X -> patient left   (L)
  +Y -> patient posterior (P)
  +Z -> patient superior  (S)

NIfTI / VoxTell requires RAS:
  +X -> patient right    (R = -L)
  +Y -> patient anterior (A = -P)
  +Z -> patient superior (S)

Conversion: flip the first two axes of the affine.
  affine_ras = diag([-1, -1, 1, 1]) @ affine_lps
"""

import base64
import gzip
import logging

import nibabel as nib
import numpy as np

log = logging.getLogger(__name__)


def build_affine_lps(
    row_direction: list[float],
    col_direction: list[float],
    slice_direction: list[float],
    x_res: float,
    y_res: float,
    z_res: float,
    origin: list[float],
) -> np.ndarray:
    """
    Build a 4x4 LPS affine from Varian ESAPI image geometry.

    The affine maps voxel index (x, y, z) to LPS world coordinates (mm):
        P_lps = affine_lps @ [x, y, z, 1]^T
    """
    affine = np.eye(4, dtype=np.float64)
    affine[:3, 0] = np.array(row_direction, dtype=np.float64) * x_res
    affine[:3, 1] = np.array(col_direction, dtype=np.float64) * y_res
    affine[:3, 2] = np.array(slice_direction, dtype=np.float64) * z_res
    affine[:3, 3] = np.array(origin, dtype=np.float64)
    return affine


def decode_slice(voxel_data_b64: str, x_size: int, y_size: int) -> np.ndarray:
    """
    Decode a single 2D slice from base64(gzip(int32-LE)) to int32 array.

    Returns shape (y_size, x_size) matching ESAPI row-major (y*XSize + x).
    """
    raw_bytes = gzip.decompress(base64.b64decode(voxel_data_b64))
    expected = x_size * y_size * 4  # int32 = 4 bytes
    if len(raw_bytes) != expected:
        raise ValueError(
            f"Decoded slice length {len(raw_bytes)} does not match "
            f"expected {expected} bytes for shape ({y_size},{x_size}) int32"
        )
    return np.frombuffer(raw_bytes, dtype="<i4").reshape(y_size, x_size).copy()


def build_nifti_from_array(
    volume: np.ndarray,
    affine_lps: np.ndarray,
    output_path: str,
) -> np.ndarray:
    """
    Build a RAS-oriented NIfTI from an already-assembled int32 (Z,Y,X) array.

    Transposes to (X,Y,Z) float32, applies LPS->RAS flip, saves NIfTI.
    Returns the LPS affine for coordinate conversion during contour extraction.
    """
    # Transpose (Z, Y, X) -> (X, Y, Z) for NIfTI convention
    arr_xyz = volume.transpose(2, 1, 0).astype(np.float32)

    # Flip X and Y columns to convert LPS -> RAS
    affine_ras = np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine_lps

    img = nib.Nifti1Image(arr_xyz, affine_ras)
    img.to_filename(output_path)
    log.info("Saved NIfTI %s  shape=%s", output_path, arr_xyz.shape)

    return affine_lps

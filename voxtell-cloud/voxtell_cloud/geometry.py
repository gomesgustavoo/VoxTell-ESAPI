"""DICOM/ESAPI voxel data -> NIfTI in RAS, and the LPS affine that inverts it.

Coordinate convention
---------------------
Varian Eclipse (and DICOM) use the LPS patient coordinate system:
  +X -> patient left       (L)
  +Y -> patient posterior  (P)
  +Z -> patient superior   (S)

NIfTI / VoxTell require RAS:
  +X -> patient right      (R = -L)
  +Y -> patient anterior   (A = -P)
  +Z -> patient superior   (S)

The first two axes flip, so ``affine_ras = diag([-1, -1, 1, 1]) @ affine_lps``.
The **LPS** affine is what the job row stores: it is what maps a predicted voxel
back to a millimetre position Eclipse understands (see :mod:`contours`).

Stored values vs HU
-------------------
``Image.GetVoxels`` in ESAPI returns *stored* values, not Hounsfield units;
``Image.VoxelToDisplayValue`` applies a linear ``value * slope + intercept``.
VoxTell normalises per-image with z-score, which is invariant to that affine
rescale -- but its preprocessing first calls ``crop_to_nonzero``, which
thresholds at exactly 0. In HU, air is about -1000 and the crop keeps the body;
in raw stored values air is often 0 and the crop behaves differently. So the
client sends ``scaling_slope``/``scaling_intercept`` and the rescale happens
here, once, server-side.
"""

from __future__ import annotations

import gzip
import hashlib
import logging

import numpy as np

from .wire import VOLUME_DTYPE, VOLUME_ITEMSIZE

# nibabel is imported lazily inside write_nifti_ras: the API needs
# build_affine_lps but never writes a NIfTI, and keeping the import out of the
# module body is what lets the control-plane image stay free of it.

log = logging.getLogger(__name__)

# LPS -> RAS: negate the first two world axes.
_LPS_TO_RAS = np.diag([-1.0, -1.0, 1.0, 1.0])


def build_affine_lps(
    row_direction: list[float],
    col_direction: list[float],
    slice_direction: list[float],
    x_res: float,
    y_res: float,
    z_res: float,
    origin: list[float],
) -> np.ndarray:
    """Build the 4x4 LPS affine from Varian ESAPI image geometry.

    Maps a voxel index to LPS world millimetres::

        P_lps = affine_lps @ [x, y, z, 1]^T

    The direction vectors come straight from ``image.RowDirection`` /
    ``ColumnDirection`` / ``SliceDirection`` and the spacings from
    ``image.XRes`` / ``YRes`` / ``ZRes``.
    """
    affine = np.eye(4, dtype=np.float64)
    affine[:3, 0] = np.asarray(row_direction, dtype=np.float64) * x_res
    affine[:3, 1] = np.asarray(col_direction, dtype=np.float64) * y_res
    affine[:3, 2] = np.asarray(slice_direction, dtype=np.float64) * z_res
    affine[:3, 3] = np.asarray(origin, dtype=np.float64)
    return affine


def affine_lps_to_ras(affine_lps: np.ndarray) -> np.ndarray:
    """``diag([-1, -1, 1, 1]) @ affine_lps`` -- the NIfTI-facing affine."""
    return _LPS_TO_RAS @ np.asarray(affine_lps, dtype=np.float64)


def decode_volume(
    blob: bytes,
    x_size: int,
    y_size: int,
    z_size: int,
    *,
    scaling_slope: float = 1.0,
    scaling_intercept: float = 0.0,
    expect_content_sha256: str | None = None,
) -> np.ndarray:
    """Decode an uploaded volume blob to a float32 ``(Z, Y, X)`` array in HU.

    ``blob`` is ``gzip(int16-LE, C-order (Z, Y, X))`` -- the whole volume, not a
    slice. Raises ``ValueError`` on any length mismatch so a truncated or
    mis-shaped upload fails loudly instead of segmenting garbage.

    ``expect_content_sha256`` is the digest the client computed over the
    *uncompressed* stream, checked here because this is the one place those bytes
    already exist in full. It matters because that digest is a volume's dedup
    identity: if it were wrong, a later upload of a *different* series could match
    it and be served this volume, and a planner would receive contours belonging
    to another patient's CT. Verifying it costs one pass over memory we have
    already paid for.
    """
    raw = gzip.decompress(blob)
    expected = x_size * y_size * z_size * VOLUME_ITEMSIZE
    if len(raw) != expected:
        raise ValueError(
            f"volume is {len(raw)} bytes, expected {expected} for "
            f"({z_size},{y_size},{x_size}) int16"
        )
    if expect_content_sha256:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expect_content_sha256.lower():
            # Before the float32 allocation below, which is 2x this buffer again.
            raise ValueError(
                "uploaded volume does not match its declared content hash "
                f"(expected {expect_content_sha256[:16]}..., got {actual[:16]}...) — "
                "the series was not stored correctly and must be uploaded again"
            )
    arr = np.frombuffer(raw, dtype=VOLUME_DTYPE).reshape(z_size, y_size, x_size)
    # Always produce a fresh, writable float32 array: frombuffer is read-only and
    # the caller transposes/scales it.
    out = arr.astype(np.float32)
    if scaling_slope != 1.0 or scaling_intercept != 0.0:
        out *= np.float32(scaling_slope)
        out += np.float32(scaling_intercept)
    return out


def write_nifti_ras(
    volume_zyx: np.ndarray,
    affine_lps: np.ndarray,
    output_path: str,
) -> str:
    """Write ``volume_zyx`` as a RAS-oriented NIfTI and return the path.

    Transposes ``(Z, Y, X)`` -> ``(X, Y, Z)`` for the NIfTI axis convention and
    flips the affine's first two world axes. The LPS affine itself is left
    untouched -- it is the inverse map used to project predictions back.
    """
    import nibabel as nib

    arr_xyz = np.ascontiguousarray(volume_zyx.transpose(2, 1, 0), dtype=np.float32)
    img = nib.Nifti1Image(arr_xyz, affine_lps_to_ras(affine_lps))
    img.to_filename(output_path)
    log.info("wrote %s shape=%s", output_path, arr_xyz.shape)
    return output_path


def voxels_to_lps(vox_xyz: np.ndarray, affine_lps: np.ndarray) -> np.ndarray:
    """Project ``(N, 3)`` voxel indices to ``(N, 3)`` LPS millimetres."""
    vox = np.asarray(vox_xyz, dtype=np.float64)
    homogeneous = np.ones((vox.shape[0], 4), dtype=np.float64)
    homogeneous[:, :3] = vox
    return (homogeneous @ np.asarray(affine_lps, dtype=np.float64).T)[:, :3]

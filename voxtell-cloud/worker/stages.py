"""The two CPU stages, written to run inside a ``ProcessPoolExecutor``.

Both take and return small, picklable values — paths and counts, never arrays.
Everything bulky moves through the job's scratch directory on disk, which keeps
inter-process traffic near zero for volumes that are hundreds of megabytes.

Heavy imports (nnU-Net, nibabel, skimage) happen inside the functions, not at
module scope: pool workers are spawned, and a module-level torch import would
cost every child several seconds and a few hundred megabytes even for jobs that
never reach that stage.
"""

from __future__ import annotations

import gzip
import json
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from voxtell_cloud.contours import PromptResult, extract_contours
from voxtell_cloud.geometry import decode_volume, write_nifti_ras
from voxtell_cloud.wire import result_envelope

from . import s3
from .settings import settings

log = logging.getLogger("worker.stages")


@dataclass
class Prepared:
    """What preprocessing leaves on disk for the GPU stage."""

    scratch_dir: str
    data_path: str  # (1, Z, Y, X) float32 RAS, ready for the predictor
    props_path: str  # NibabelIOWithReorient props, for the inverse reorientation
    shape: tuple[int, int, int]


def scratch_for(job_id) -> Path:
    return Path(settings.WORKER_SCRATCH_DIR) / str(job_id)


# --------------------------------------------------------------------------- #
# Stage 1 — preprocess (CPU pool)
# --------------------------------------------------------------------------- #
def preprocess(job_id: str, volume_key: str, geometry: dict) -> Prepared:
    """S3 -> HU volume -> RAS NIfTI -> the array the predictor consumes.

    Reading the NIfTI back through nnU-Net's own reader rather than keeping the
    array we just built is deliberate: ``NibabelIOWithReorient`` is the exact
    reader VoxTell was trained with, and its ``props`` are what
    ``write_seg`` later needs to put masks back in DICOM orientation. Round
    -tripping through the file makes the forward and inverse transforms provably
    each other's inverse.
    """
    from nnunetv2.imageio.nibabel_reader_writer import NibabelIOWithReorient

    work = scratch_for(job_id)
    work.mkdir(parents=True, exist_ok=True)

    blob_path = work / "volume.bin.gz"
    s3.download(volume_key, blob_path)

    volume_zyx = decode_volume(
        blob_path.read_bytes(),
        x_size=geometry["x_size"],
        y_size=geometry["y_size"],
        z_size=geometry["z_size"],
        scaling_slope=float(geometry.get("scaling_slope", 1.0)),
        scaling_intercept=float(geometry.get("scaling_intercept", 0.0)),
        # Present only for volume-backed jobs; the API copies it into the job's
        # geometry JSONB, so no worker schema change was needed for this.
        expect_content_sha256=geometry.get("content_sha256"),
    )
    blob_path.unlink(missing_ok=True)  # the compressed copy is dead weight now

    nifti_path = str(work / "volume.nii.gz")
    write_nifti_ras(volume_zyx, np.asarray(geometry["affine_lps"]), nifti_path)
    del volume_zyx

    data, props = NibabelIOWithReorient().read_images([nifti_path])

    data_path = work / "data.npy"
    np.save(data_path, np.ascontiguousarray(data, dtype=np.float32))
    props_path = work / "props.pkl"
    props_path.write_bytes(pickle.dumps(props))

    log.info("job %s preprocessed: data %s", job_id, tuple(data.shape))
    return Prepared(
        scratch_dir=str(work),
        data_path=str(data_path),
        props_path=str(props_path),
        shape=tuple(int(v) for v in data.shape[1:]),
    )


# --------------------------------------------------------------------------- #
# Stage 3 — postprocess (CPU pool)
# --------------------------------------------------------------------------- #
def postprocess(
    job_id: str,
    prepared: Prepared,
    masks_path: str,
    prompts: list[str],
    affine_lps: list,
    result_key: str,
    mask_key: str | None,
) -> dict:
    """Masks -> DICOM-oriented -> LPS contours -> gzipped JSON in S3.

    Prompts are handled one at a time and each intermediate is dropped
    immediately: a five-prompt abdominal CT would otherwise hold several
    hundred megabytes of masks in memory at once for no reason.
    """
    import nibabel as nib
    from nnunetv2.imageio.nibabel_reader_writer import NibabelIOWithReorient

    work = Path(prepared.scratch_dir)
    props = pickle.loads(Path(prepared.props_path).read_bytes())
    masks = np.load(masks_path, mmap_mode="r")
    writer = NibabelIOWithReorient()

    affine = np.asarray(affine_lps, dtype=np.float64)
    results: list[PromptResult] = []
    mask_stack: list[np.ndarray] = []

    for i, prompt in enumerate(prompts):
        tmp = work / f"mask_{i}.nii.gz"
        # write_seg reverses the reorientation applied at read time, so what
        # lands on disk is aligned with the volume the client uploaded.
        writer.write_seg(np.asarray(masks[i], dtype=np.uint8), str(tmp), props)
        # asanyarray(dataobj) keeps the on-disk uint8; get_fdata() would inflate
        # a 50 MB mask into a 400 MB float64 array.
        dicom_mask = np.asanyarray(nib.load(str(tmp)).dataobj).astype(np.uint8, copy=False)

        results.append(
            PromptResult(
                prompt=prompt,
                voxel_count=int(np.count_nonzero(dicom_mask)),
                contours=extract_contours(dicom_mask, affine),
            )
        )
        if mask_key:
            # Back to the client's own (Z, Y, X) upload layout.
            mask_stack.append(np.ascontiguousarray(dicom_mask.transpose(2, 1, 0)))
        tmp.unlink(missing_ok=True)

    envelope = result_envelope(
        job_id=str(job_id),
        model=Path(settings.INFER_MODEL_DIR).name or "voxtell",
        prompts=prompts,
        results=[r.to_json() for r in results],
    )
    payload = gzip.compress(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))
    s3.upload_bytes(payload, result_key, content_type="application/gzip")

    if mask_key:
        # (P, Z, Y, X) even for a single prompt, so the client's unpacking code
        # does not need a special case.
        stacked = np.stack(mask_stack)
        s3.upload_bytes(gzip.compress(stacked.tobytes(order="C")), mask_key)

    total_contours = sum(len(r.contours) for r in results)
    log.info(
        "job %s postprocessed: %d prompt(s), %d contour(s), %d result bytes",
        job_id, len(results), total_contours, len(payload),
    )
    return {
        "result_bytes": len(payload),
        "contours": total_contours,
        "voxels": [r.voxel_count for r in results],
    }

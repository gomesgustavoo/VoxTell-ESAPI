"""Model lifecycle and the GPU stage.

Everything here runs on the single GPU thread. The actual prediction is upstream
``voxtell.server.runner.RemoteInferenceEngine``, which already implements the
hard parts — sizing the prompt batch to free VRAM, halving it on OOM, falling
back to CPU logits accumulation, optional largest-component cleanup — so we
neither copy nor re-derive that logic.

Two things we add on top:

* the cross-service **GPU mutex** (see :mod:`gpu_lock`), held only around the
  prediction call, so DicomSegVR's inference pod and this one never occupy VRAM
  simultaneously;
* the **persistent embedding cache** (see :mod:`embeddings`), so a free-text
  prompt costs one Qwen3 backbone pass ever, not one per worker restart.
"""

from __future__ import annotations

import gc
import logging
import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import embeddings as embedding_cache
from . import gpu_lock
from .settings import settings

log = logging.getLogger("worker.engine")

# Set by warm_up(); only ever touched from the GPU thread.
_predictor = None
_engine = None
# Prompts already in the DB, so we persist only what a job newly computed.
_known_prompts: set[str] = set()


class Cancelled(RuntimeError):
    """The job was cancelled between sliding-window patches."""


def _load_bank() -> dict[str, np.ndarray] | None:
    """Published bank (from the model store) merged with our own DB cache."""
    bank: dict[str, np.ndarray] = {}

    path = settings.INFER_EMBEDDING_BANK
    if path and Path(path).exists():
        from voxtell.utils.embedding_bank import load_embedding_bank

        bank.update(load_embedding_bank(path))
        log.info("loaded %d published embedding(s) from %s", len(bank), path)
    elif path:
        log.warning(
            "embedding bank %s not found — free-text prompts will load the "
            "Qwen3 backbone until the cache fills",
            path,
        )

    try:
        bank.update(embedding_cache.load_all())
    except Exception as exc:
        # A cold or unreachable cache must not stop the worker from starting.
        log.warning("could not load the embedding cache: %s", exc)

    _known_prompts.update(bank)
    return bank or None


def warm_up() -> None:
    """Construct the predictor and the inference engine. Runs on the GPU thread."""
    global _predictor, _engine
    if _predictor is not None:
        return

    import torch
    from voxtell.inference.predictor import VoxTellPredictor
    from voxtell.server.runner import RemoteInferenceEngine

    device_str = settings.device_str
    if device_str.startswith("cuda") and not torch.cuda.is_available():
        log.warning("CUDA unavailable — falling back to CPU (inference will be slow)")
        device_str = "cpu"

    model_dir = settings.INFER_MODEL_DIR or None
    bank = _load_bank()

    log.info("loading VoxTell from %s on %s", model_dir or "<hugging face>", device_str)
    started = time.monotonic()
    _predictor = VoxTellPredictor(
        model_dir=model_dir,
        device=torch.device(device_str),
        embedding_bank=bank,
        # An explicit bank wins anyway; False also stops the constructor from
        # reaching out to Hugging Face, which a locked-down pod cannot do.
        use_precomputed_embeddings=False,
    )
    _engine = RemoteInferenceEngine(_predictor)
    log.info(
        "VoxTell warm on %s in %.1fs (patch %s, %d cached prompt(s))",
        device_str, time.monotonic() - started, _predictor.patch_size, len(_known_prompts),
    )


def _persist_new_embeddings() -> None:
    """Write back whatever the predictor computed for prompts we had not seen."""
    bank = getattr(_predictor, "embedding_bank", None) or {}
    fresh = {p: v for p, v in bank.items() if p not in _known_prompts}
    if not fresh:
        return
    try:
        embedding_cache.persist(fresh)
        _known_prompts.update(fresh)
    except Exception as exc:
        # Losing the cache write only costs time on a later job.
        log.warning("could not persist %d embedding(s): %s", len(fresh), exc)


def _release_text_backbone() -> None:
    """Drop the ~8 GB Qwen3 backbone from host RAM.

    Upstream already moves it off the GPU after embedding, but it stays resident
    on the CPU. On a 23 GB box shared with another inference pod that is too much
    to hold idle, and with the embedding cache in front of it a reload is rare.
    """
    if _predictor is None or getattr(_predictor, "text_backbone", None) is None:
        return
    _predictor.text_backbone = None
    _predictor.tokenizer = None
    gc.collect()
    log.info("released the text backbone from host memory")


def segment(
    data_path: str,
    prompts: list[str],
    keep_largest: bool,
    masks_path: str,
    progress: Callable[[float, str], None],
    is_cancelled: Callable[[], bool],
    lease=None,
) -> float:
    """Run one job on the GPU. Returns GPU-held wall time in seconds.

    Masks are written to ``masks_path`` rather than returned so the postprocess
    stage can pick them up in another process without pickling a few hundred
    megabytes through a pipe.
    """
    if _engine is None:
        raise RuntimeError("warm_up() has not run")

    from voxtell.inference.predictor import InferenceCancelled

    data = np.load(data_path, mmap_mode="r")

    def on_patch(done: int, total: int) -> bool:
        # Inference spans 0.20 -> 0.70 of the job's overall progress bar; the
        # bands either side belong to preprocess and postprocess.
        frac = 0.20 + 0.50 * (done / max(total, 1))
        progress(frac, f"Segmenting ({done}/{total} patches)")
        # Returning False is upstream's cancellation signal.
        return not is_cancelled()

    def on_notice(message: str) -> None:
        # OOM fallbacks and batch-size reductions — worth showing the user, since
        # they explain why a job is slower than usual. Also counts as progress: a
        # VRAM-halving retry is the engine working, not stalling.
        log.info("engine notice: %s", message)
        progress(0.20, message)

    def on_wait(_seconds: float) -> None:
        # Entering the GPU queue. The watchdog phase changes so blocking on
        # pg_advisory_lock gets its own much larger grace budget — waiting your turn
        # behind DicomSegVR is legitimate non-progress, and without this the wait would
        # be indistinguishable from a wedged job and we would recycle our own pod for
        # being polite.
        if lease is not None:
            from . import watchdog

            lease.phase(watchdog.PHASE_WAITING_FOR_GPU)
        progress(0.16, "Waiting for the GPU")

    started = time.monotonic()
    try:
        with gpu_lock.gpu_lock(on_wait=on_wait):
            # Acquired: back to the compute budget, where silence is suspicious fast.
            if lease is not None:
                from . import watchdog

                lease.phase(watchdog.PHASE_COMPUTE)
            gpu_started = time.monotonic()
            masks = _engine.segment(
                np.asarray(data),
                prompts,
                keep_largest,
                progress_callback=on_patch,
                notice_callback=on_notice,
            )
            gpu_seconds = time.monotonic() - gpu_started
    except InferenceCancelled as exc:
        raise Cancelled(str(exc) or "cancelled") from exc
    finally:
        _persist_new_embeddings()
        _release_text_backbone()

    np.save(masks_path, np.asarray(masks, dtype=np.uint8))
    log.info(
        "job masks %s written in %.1fs (%.1fs on the GPU)",
        tuple(masks.shape), time.monotonic() - started, gpu_seconds,
    )
    return gpu_seconds

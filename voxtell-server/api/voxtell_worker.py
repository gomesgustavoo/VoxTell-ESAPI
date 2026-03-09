"""
Singleton VoxTellPredictor wrapper with async serialization.

A single model instance is shared across all requests. An asyncio.Lock ensures
only one inference runs at a time (GPU is single-threaded). The blocking prediction
work is offloaded to a thread-pool executor so the event loop stays responsive
for polling requests during the 1-5 minute inference window.
"""

import asyncio
import gc
import logging
from typing import Any

import numpy as np
import torch

from nnunetv2.imageio.nibabel_reader_writer import NibabelIOWithReorient

from voxtell.inference.predictor import VoxTellPredictor

log = logging.getLogger(__name__)


class VoxTellWorker:
    def __init__(self) -> None:
        self._predictor: VoxTellPredictor | None = None
        self._lock = asyncio.Lock()

    @property
    def model_loaded(self) -> bool:
        return self._predictor is not None

    def initialize(self, model_dir: str, device_str: str, text_model: str) -> None:
        """Load the model. Called once at application startup (blocking)."""
        log.info("Loading VoxTell model from %s on %s ...", model_dir, device_str)
        self._predictor = VoxTellPredictor(
            model_dir=model_dir,
            device=torch.device(device_str),
            text_encoding_model=text_model,
        )
        self._predictor.perform_everything_on_device = False
        log.info("Model loaded (perform_everything_on_device=False)")

    async def predict(
        self, nifti_path: str, prompts: list[str]
    ) -> tuple[np.ndarray, Any]:
        """
        Run inference asynchronously.

        Acquires the inference lock so only one job runs at a time, then
        dispatches the blocking work to the default thread-pool executor.

        Returns:
            masks  - np.ndarray shape (num_prompts, X, Y, Z), dtype uint8
            props  - image properties dict from NibabelIOWithReorient
        """
        if self._predictor is None:
            raise RuntimeError("Model not initialised — call initialize() first")

        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._predict_sync, nifti_path, prompts
            )

    def _predict_sync(
        self, nifti_path: str, prompts: list[str]
    ) -> tuple[np.ndarray, Any]:
        """Blocking inference — runs in a thread-pool worker."""
        log.info("Starting inference: prompts=%s", prompts)
        try:
            reader = NibabelIOWithReorient()
            img, props = reader.read_images([nifti_path])
            masks = self._predictor.predict_single_image(img, prompts)
            log.info("Inference complete: masks shape=%s", masks.shape)
            return masks, props
        finally:
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                log.debug("Cleared CUDA cache after inference")


# Module-level singleton imported by main.py
worker = VoxTellWorker()

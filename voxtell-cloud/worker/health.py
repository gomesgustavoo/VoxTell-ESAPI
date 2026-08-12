"""Liveness touch-file.

The GPU worker exposes no HTTP port, so the Kubernetes liveness probe is an
``exec`` that checks the mtime of this file. The heartbeat coroutine touches it
every ``WORKER_HEARTBEAT_SECONDS`` — it runs on the event loop, which stays
responsive even while a job occupies the GPU thread, so a stuck event loop (the
failure a liveness probe should actually catch) is what makes it go stale.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .settings import settings

log = logging.getLogger("worker.health")


def touch() -> None:
    try:
        path = Path(settings.WORKER_ALIVE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, None)
    except Exception as exc:  # never let the probe file kill the worker
        log.warning("could not touch %s: %s", settings.WORKER_ALIVE_FILE, exc)

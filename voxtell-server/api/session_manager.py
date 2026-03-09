import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class SessionData:
    session_id: str
    nifti_path: str
    affine_lps: list  # 4x4 affine as nested list
    original_shape: tuple[int, int, int]  # (x_size, y_size, z_size)
    volume_buffer: np.ndarray | None = None
    slices_received: set[int] = field(default_factory=set)
    finalized: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class JobData:
    job_id: str
    session_id: str
    prompts: list[str]
    status: str  # "pending" | "running" | "completed" | "failed"
    results: list | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Module-level stores — single-process only (--workers 1 required)
sessions: dict[str, SessionData] = {}
jobs: dict[str, JobData] = {}


def get_session(session_id: str) -> SessionData | None:
    return sessions.get(session_id)


def get_job(job_id: str) -> JobData | None:
    return jobs.get(job_id)


def delete_session(session_id: str) -> bool:
    """Remove session from store and delete its files. Returns True if found."""
    session = sessions.pop(session_id, None)
    if session is None:
        return False

    # Clean up orphaned jobs referencing this session
    orphaned = [jid for jid, j in jobs.items() if j.session_id == session_id]
    for jid in orphaned:
        del jobs[jid]
    if orphaned:
        log.info(
            "Cleaned up %d orphaned job(s) for session %s", len(orphaned), session_id
        )

    session_dir = Path(session.nifti_path).parent
    shutil.rmtree(session_dir, ignore_errors=True)
    log.info("Deleted session %s", session_id)
    return True


async def cleanup_loop() -> None:
    """Background task: periodically removes expired sessions and jobs."""
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        now = datetime.now(timezone.utc)

        expired_sessions = 0
        for sid in list(sessions):
            s = sessions[sid]
            if (now - s.created_at).total_seconds() > settings.session_ttl_seconds:
                delete_session(sid)
                expired_sessions += 1

        expired_jobs = 0
        job_ttl = settings.session_ttl_seconds * 2
        for jid in list(jobs):
            j = jobs[jid]
            if (now - j.created_at).total_seconds() > job_ttl:
                del jobs[jid]
                expired_jobs += 1

        if expired_sessions or expired_jobs:
            log.info(
                "Cleanup: removed %d session(s), %d job(s); %d session(s) and %d job(s) remaining",
                expired_sessions,
                expired_jobs,
                len(sessions),
                len(jobs),
            )

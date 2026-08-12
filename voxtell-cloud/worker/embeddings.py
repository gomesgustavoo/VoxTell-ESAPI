"""Persistent prompt-embedding cache.

VoxTell embeds each free-text prompt with a frozen Qwen3-Embedding-4B backbone.
Upstream ships a precomputed bank for its own label set and only loads the
backbone (about 8 GB) for prompts outside it — but our users type free text, so
misses are the normal case, and without persistence every worker restart would
pay that cost again for prompts we have already seen.

This table extends upstream's in-memory bank across restarts: load everything at
startup into ``predictor.embedding_bank``, and after each job persist whatever
new entries the predictor computed. Embedding is deterministic for a given
string, so a cached vector is exactly what the backbone would have produced.

Vectors are stored as raw float16 bytes, matching the dtype upstream uses for
both the published bank and its own writebacks.
"""

from __future__ import annotations

import logging

import numpy as np
from sqlalchemy import text

from . import db

log = logging.getLogger("worker.embeddings")

_DTYPE = np.float16


def load_all() -> dict[str, np.ndarray]:
    """Every cached prompt -> vector. Keys are lowercase, as the predictor expects."""
    with db.get_engine().begin() as conn:
        rows = conn.execute(text("SELECT prompt, dim, vec FROM prompt_embeddings")).fetchall()
    bank: dict[str, np.ndarray] = {}
    for prompt, dim, vec in rows:
        arr = np.frombuffer(vec, dtype=_DTYPE)
        if arr.size != dim:
            log.warning("skipping malformed cached embedding for %r", prompt)
            continue
        bank[prompt] = arr
    if bank:
        log.info("loaded %d cached prompt embedding(s)", len(bank))
    return bank


def persist(new_entries: dict[str, np.ndarray]) -> int:
    """Upsert freshly computed embeddings. Safe to call with entries we already have."""
    if not new_entries:
        return 0
    rows = [
        {
            "prompt": prompt[:256],
            "dim": int(np.asarray(vec).size),
            "vec": np.asarray(vec, dtype=_DTYPE).tobytes(),
        }
        for prompt, vec in new_entries.items()
    ]
    with db.get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO prompt_embeddings (prompt, dim, vec) "
                "VALUES (:prompt, :dim, :vec) ON CONFLICT (prompt) DO NOTHING"
            ),
            rows,
        )
    log.info("persisted %d new prompt embedding(s)", len(rows))
    return len(rows)

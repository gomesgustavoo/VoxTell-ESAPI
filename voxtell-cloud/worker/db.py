"""Sync SQLAlchemy engine for the worker.

Raw SQL against the schema the API owns (``api/models.py``). The worker never
migrates — deploy order is API first, worker second.

Pool is deliberately tiny: this process runs one job at a time on the GPU and a
couple of CPU stages, so more connections would just idle.

The engine is built **lazily** by ``get_engine()`` rather than at module import.
That matters for testability, not style: this module previously created the engine
at import time from ``settings``, which reads the environment at *its* import, so
``import worker.job`` permanently bound the queue code to whatever ``DB_*`` values
happened to be set in the interpreter. A test could not point the claim SQL at a
scratch schema, which is why none of the queue behaviour had tests.

Two rules, both load-bearing for that:

* Do not reintroduce a module-level ``engine``.
* Call it as ``db.get_engine()`` after ``from . import db`` — never
  ``from .db import get_engine``. A direct name import copies the function into the
  caller's namespace, so a test would have to patch every module separately. Going
  through the module keeps ``worker.db.get_engine`` the single seam.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .settings import settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine, created on first use and cached thereafter."""
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=3,
        echo=False,
    )


def reset_engine() -> None:
    """Drop the cached engine, disposing its pool. Tests only.

    Lets a fixture rebind the worker to a different database after ``settings``
    has been repointed. Never call this from production code — an in-flight job
    holding a connection from the old pool would be surprised.
    """
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()

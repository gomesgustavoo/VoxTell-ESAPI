"""QA baselines: record what a model produced, so a later run can be scored.

This is the *recording half* of the two-run workflow. Run 1 segments, writes into
Eclipse, and posts the AI contours here as the "before". Run 2 will read the
planner's edited structures back and the server will score them against this row.

Why the recording ships before any verdict UI exists
----------------------------------------------------
The measurement rides on work the clinic was doing anyway -- but only if the
snapshot was taken at the time. An edit made before this endpoint exists can never
be measured afterwards, because the model's original output is gone. Every day
without recording is a day of unrecoverable data, which is why this is not
sequenced after the feature that displays it.

Idempotency, and why it is not optional
---------------------------------------
The natural trigger is "the plugin opened a series it recognises", so a planner
who opens a patient, glances, and closes it will post the same snapshot repeatedly
before editing anything. That is the normal case. So:

* Dedup is on ``(user, series_key, structure_set_sha256)`` -- the same *contours*,
  not merely the same names, since hashing ids alone would call a fully redrawn
  organ unchanged.
* A genuinely different snapshot **supersedes** its predecessor instead of
  appending, so "the baseline for this series" is always one row.
* Nothing is billed here. Recording a baseline is a side effect of a job that was
  already paid for.
"""

from __future__ import annotations

import gzip
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_caller
from ..config import settings
from ..db import get_session
from ..lineage import lineage_secret_for
from ..models import QaBaseline, User, utcnow
from ..schemas import BaselineResponse, SnapshotRequest
from ..storage import baseline_contours_key, put_bytes

router = APIRouter(prefix="/qa", tags=["qa"])

# A snapshot is contours and geometry, never pixels, so it is small. This cap is a
# tripwire against a malformed client rather than a real limit: 40 structures on a
# 200-slice CT gzip to a few hundred kilobytes.
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


@router.post(
    "/baselines", response_model=BaselineResponse, status_code=status.HTTP_201_CREATED
)
async def create_baseline(
    snapshot: SnapshotRequest,
    job_id: uuid.UUID | None = Query(None, description="The job that produced these contours"),
    user: User = Depends(get_caller),
    session: AsyncSession = Depends(get_session),
) -> BaselineResponse:
    """Record a structure snapshot as the QA baseline for its series."""
    if lineage_secret_for(user.id) is None:
        # The deployment has no lineage secret, so the keys the client computed
        # cannot be ones we issued. Refusing is honest; accepting would store rows
        # nothing could ever link.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"error": "qa_lineage_disabled",
                    "message": "This deployment has QA lineage disabled."},
        )

    if not snapshot.structures:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "empty_snapshot",
                    "message": "A snapshot needs at least one structure."},
        )

    # --- idempotency: has this exact structure set already been recorded? --- #
    existing = (
        await session.execute(
            select(QaBaseline)
            .where(
                QaBaseline.user_id == user.id,
                QaBaseline.series_key == snapshot.series_key,
                QaBaseline.structure_set_sha256 == snapshot.structure_set_sha256,
                QaBaseline.state != "superseded",
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None:
        return _response(existing, created=False, superseded=False,
                         message="Already recorded; nothing changed.")

    # --- store the contours outside jobs/ and volumes/ --- #
    payload = gzip.compress(
        json.dumps(
            snapshot.model_dump(mode="json", by_alias=True),
            separators=(",", ":"),
        ).encode("utf-8"),
        compresslevel=6,
    )
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "snapshot_too_large",
                    "message": f"Snapshot is {len(payload)} bytes compressed."},
        )

    key = baseline_contours_key(
        user.id, snapshot.series_key, snapshot.structure_set_sha256
    )
    await put_bytes(key, payload, content_type="application/gzip")

    # --- supersede any earlier, different baseline for this series --- #
    previous = (
        await session.execute(
            select(QaBaseline)
            .where(
                QaBaseline.user_id == user.id,
                QaBaseline.series_key == snapshot.series_key,
                QaBaseline.state != "superseded",
            )
            .with_for_update()
        )
    ).scalars().all()

    record = QaBaseline(
        user_id=user.id,
        job_id=job_id,
        state="provisional",
        series_key=snapshot.series_key,
        for_key=snapshot.for_key,
        scanner_key=snapshot.scanner_key,
        structure_set_sha256=snapshot.structure_set_sha256,
        structure_set_uid=snapshot.structure_set_uid,
        contours_key=key,
        contours_bytes=len(payload),
        structure_count=len(snapshot.structures),
        models=[],
        geometry=(
            snapshot.geometry.model_dump(mode="json") if snapshot.geometry else {}
        ),
    )

    # Every structure in the snapshot is already approved in Eclipse => the set is
    # settled and the record can skip `provisional`. Reading Eclipse's own approval
    # state beats waiting an arbitrary number of days for one to elapse.
    if all(s.is_approved for s in snapshot.structures):
        record.state = "confirmed"
        record.confirmed_at = utcnow()

    # ORDER IS LOAD-BEARING. `ux_qa_baselines_live` is UNIQUE on
    # (user_id, series_key) WHERE state <> 'superseded', and SQLAlchemy's unit of
    # work emits INSERTs before UPDATEs within a mapper. Adding the new row first
    # would therefore hit the index while the previous row is still live and fail
    # with a duplicate-key error on the perfectly ordinary "planner edited and came
    # back" path. So the supersede is flushed on its own, first.
    now = utcnow()
    for row in previous:
        row.state = "superseded"
        row.superseded_at = now
    if previous:
        await session.flush()

    session.add(record)
    await session.flush()

    # Only now that the new row has an id can the old ones point at it.
    for row in previous:
        row.superseded_by = record.id

    # Committed here, not left to the dependency teardown: the client may post the
    # same snapshot again immediately, and the dedup lookup above has to see it.
    await session.commit()

    return _response(
        record,
        created=True,
        superseded=bool(previous),
        message="Recorded." if not previous else "Recorded; replaced the previous snapshot.",
    )


def _response(
    record: QaBaseline, created: bool, superseded: bool, message: str | None
) -> BaselineResponse:
    return BaselineResponse(
        baseline_id=record.id,
        state=record.state,
        created=created,
        superseded=superseded,
        structure_count=record.structure_count,
        web_url=_web_url(record.id),
        message=message,
    )


def _web_url(baseline_id: uuid.UUID) -> str | None:
    """Deep link to the coloured comparison in the console, when one is configured.

    None when unset, and the plugin then shows no link at all. A link that 404s on a
    workstation with no browser access to the console is worse than no link.
    """
    base = (settings.VOXTELL_CONSOLE_URL or "").rstrip("/")
    return f"{base}/dashboard/#/qa/{baseline_id}" if base else None

"""S3 (SeaweedFS) access via two boto3 clients.

  * presign client -> S3_ENDPOINT_PUBLIC  (SigV4 signs Host; the signed host must
    be the one the ESAPI client actually connects to)
  * ops client     -> S3_ENDPOINT_INTERNAL (create/complete/abort/list/delete)

Both use path-style addressing: SeaweedFS 405s on virtual-host style, and SigV4
breaks if anything in the chain rewrites Host.

boto3 is synchronous, so every blocking call is pushed through
``asyncio.to_thread`` — the event loop must stay free to serve status polls.
"""

from __future__ import annotations

import asyncio

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import settings

_boto_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    retries={"max_attempts": 3, "mode": "standard"},
)

OCTET_STREAM = "application/octet-stream"


def _make_client(endpoint_url: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=_boto_config,
    )


_presign_client = _make_client(settings.S3_ENDPOINT_PUBLIC)
_ops_client = _make_client(settings.S3_ENDPOINT_INTERNAL)

BUCKET = settings.S3_BUCKET


# --------------------------------------------------------------------------- #
# Key layout
# --------------------------------------------------------------------------- #
def job_prefix(user_id, job_id) -> str:
    """All objects for a job live under one prefix so purging is one call."""
    return f"u/{user_id}/jobs/{job_id}/"


def volume_key(user_id, job_id) -> str:
    """Legacy inline-upload volume: lives under the job that uploaded it."""
    return job_prefix(user_id, job_id) + "volume.bin.gz"


def volume_prefix(user_id) -> str:
    return f"u/{user_id}/volumes/"


def shared_volume_key(user_id, content_sha256: str) -> str:
    """A reusable volume's object key — deliberately NOT under ``jobs/``.

    This placement is the single most important structural decision in the
    upload-once design. Four separate code paths purge by
    ``delete_prefix(job_prefix(user_id, job_id))``: job delete, job cancel, the
    submit size-mismatch bail-out, and the sweeper's result expiry. Keeping a
    shared volume outside that subtree makes all four automatically correct with
    no edits, rather than correct only as long as nobody forgets.

    Content-addressed, so completing the same upload twice is naturally
    idempotent — same key, same bytes. Note the consequence: two rows (same
    content, different geometry) can share one key, so the sweeper deletes the
    object only when no live row still references it.

    ``tests/test_storage_keys.py`` pins the not-under-jobs property.
    """
    return volume_prefix(user_id) + f"{content_sha256}.bin.gz"


def baseline_prefix(user_id) -> str:
    return f"u/{user_id}/qa/"


def baseline_contours_key(user_id, series_key: str, structure_set_sha256: str) -> str:
    """A QA baseline's contour object — deliberately NOT under ``jobs/``.

    Same reasoning as :func:`shared_volume_key`, and the same four purge paths.
    A baseline has to outlive the job that produced it and the volume it was
    computed from: the volume expires on a sliding 2-hour idle TTL under an
    8-hour ceiling and the result after 24 hours, but the planner does not come
    back to edit for days. If the "before" contours sat under ``jobs/`` they
    would be swept away exactly when the comparison finally became possible, and
    the failure would look like "QA silently never works" rather than an error.

    Keyed by ``(series_key, structure_set_sha256)`` so re-recording an identical
    structure set writes the same object — the same idempotency the DB dedup
    index enforces, arranged so the storage layer agrees with it for free.
    """
    return baseline_prefix(user_id) + f"{series_key}/{structure_set_sha256}.json.gz"


def result_key(user_id, job_id) -> str:
    return job_prefix(user_id, job_id) + "result.json.gz"


def mask_key(user_id, job_id) -> str:
    return job_prefix(user_id, job_id) + "mask.bin.gz"


# --------------------------------------------------------------------------- #
# Sync inner helpers (always called inside asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _create_multipart_sync(key: str) -> str:
    resp = _ops_client.create_multipart_upload(
        Bucket=BUCKET, Key=key, ContentType=OCTET_STREAM
    )
    return resp["UploadId"]


def _presign_parts_sync(key: str, upload_id: str, count: int, expires: int) -> list[str]:
    # Presigning is local HMAC with no network round-trip, so the whole batch
    # costs one thread hop.
    return [
        _presign_client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": BUCKET,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": n,
            },
            ExpiresIn=expires,
        )
        for n in range(1, count + 1)
    ]


def _complete_multipart_sync(key: str, upload_id: str, parts: list[dict]) -> int:
    _ops_client.complete_multipart_upload(
        Bucket=BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={
            "Parts": [
                {"PartNumber": p["part_number"], "ETag": p["etag"]}
                for p in sorted(parts, key=lambda p: p["part_number"])
            ]
        },
    )
    head = _ops_client.head_object(Bucket=BUCKET, Key=key)
    return int(head.get("ContentLength", 0))


def _abort_multipart_sync(key: str, upload_id: str) -> None:
    try:
        _ops_client.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload_id)
    except ClientError:
        # Already completed or already aborted — nothing to clean up.
        pass


def _put_bytes_sync(key: str, payload: bytes, content_type: str) -> None:
    _ops_client.put_object(
        Bucket=BUCKET, Key=key, Body=payload, ContentType=content_type
    )


def _presign_get_sync(key: str, expires: int, filename: str | None) -> str:
    params = {"Bucket": BUCKET, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return _presign_client.generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires
    )


def _object_exists_sync(key: str) -> bool:
    try:
        _ops_client.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _delete_prefix_sync(prefix: str) -> int:
    deleted = 0
    batch: list[dict] = []
    paginator = _ops_client.get_paginator("list_objects_v2")

    def flush(items: list[dict]) -> int:
        if not items:
            return 0
        _ops_client.delete_objects(Bucket=BUCKET, Delete={"Objects": items, "Quiet": True})
        return len(items)

    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            batch.append({"Key": obj["Key"]})
            if len(batch) == 1000:
                deleted += flush(batch)
                batch = []
    return deleted + flush(batch)


# --------------------------------------------------------------------------- #
# Async surface
# --------------------------------------------------------------------------- #
async def create_multipart(key: str) -> str:
    return await asyncio.to_thread(_create_multipart_sync, key)


async def presign_parts(key: str, upload_id: str, count: int) -> list[str]:
    return await asyncio.to_thread(
        _presign_parts_sync, key, upload_id, count,
        settings.VOXTELL_PRESIGN_EXPIRY_SECONDS,
    )


async def complete_multipart(key: str, upload_id: str, parts: list[dict]) -> int:
    """Finish the upload and return the assembled object's size in bytes."""
    return await asyncio.to_thread(_complete_multipart_sync, key, upload_id, parts)


async def abort_multipart(key: str, upload_id: str) -> None:
    await asyncio.to_thread(_abort_multipart_sync, key, upload_id)


async def presign_get(key: str, filename: str | None = None) -> str:
    return await asyncio.to_thread(
        _presign_get_sync, key, settings.VOXTELL_RESULT_EXPIRY_SECONDS, filename
    )


async def put_bytes(key: str, payload: bytes, content_type: str = "application/octet-stream") -> None:
    """Upload a small object in one request.

    Deliberately not multipart. A QA snapshot is contours and geometry only --
    kilobytes -- so the presign/PUT/complete dance the volume path needs would be
    ceremony, and it writes server-side where there is no Cloudflare body cap to
    work around.
    """
    await asyncio.to_thread(_put_bytes_sync, key, payload, content_type)


async def object_exists(key: str) -> bool:
    return await asyncio.to_thread(_object_exists_sync, key)


async def delete_prefix(prefix: str) -> int:
    return await asyncio.to_thread(_delete_prefix_sync, prefix)

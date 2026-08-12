"""S3 (SeaweedFS) helpers for the worker — internal endpoint, sync boto3.

Mirrors the API's storage module minus the presign client: the worker never
talks to a browser or an Eclipse workstation, only to the object store.
"""

from __future__ import annotations

import logging
from pathlib import Path

import boto3
from botocore.config import Config

from .settings import settings

log = logging.getLogger("worker.s3")

_boto_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    retries={"max_attempts": 3, "mode": "standard"},
)


def client():
    """Build a client on demand.

    Not a module-level singleton: the CPU stages run in forked/spawned pool
    workers, and a boto3 client created before the fork is not safe to reuse
    across it.
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_INTERNAL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=_boto_config,
    )


BUCKET = settings.S3_BUCKET


def download(key: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    client().download_file(BUCKET, key, str(dest))
    size = dest.stat().st_size
    log.info("downloaded %s (%d bytes)", key, size)
    return size


def download_bytes(key: str) -> bytes:
    return client().get_object(Bucket=BUCKET, Key=key)["Body"].read()


def upload(src: Path, key: str, content_type: str = "application/gzip") -> None:
    client().upload_file(str(src), BUCKET, key, ExtraArgs={"ContentType": content_type})
    log.info("uploaded %s (%d bytes)", key, src.stat().st_size)


def upload_bytes(data: bytes, key: str, content_type: str = "application/gzip") -> None:
    client().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)
    log.info("uploaded %s (%d bytes)", key, len(data))


def delete(key: str) -> None:
    client().delete_object(Bucket=BUCKET, Key=key)

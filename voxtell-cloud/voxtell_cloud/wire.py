"""Wire format shared by the ESAPI client, the API and the worker.

Everything the C# client sends or receives is **gzip** — not blosc2 — so the
plugin needs nothing beyond ``System.IO.Compression.GZipStream`` and a JSON
library. blosc2 stays an internal, server-side detail.

Volume upload (client -> S3, one object per job)
------------------------------------------------
``gzip( int16-LE voxels, C-order (Z, Y, X) )``

int16 rather than v1's int32: CT stored values and HU both fit, and it halves
the wire size. The volume carries **no header** — geometry travels as JSON in
``POST /v1/jobs`` so the API can validate it before a single byte is uploaded.

The object is written with S3 multipart so that no single HTTP request exceeds
Cloudflare's 100 MB body cap and an interrupted upload can resume part-wise.
S3 requires every part except the last to be at least 5 MiB.

Result download (worker -> S3 -> client)
----------------------------------------
``result.json.gz`` -- gzip of :func:`result_envelope` output.
``mask.bin.gz``    -- gzip( uint8 masks, C-order (P, Z, Y, X) ), only when the
                      job asked for ``want_mask``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

# --- volume upload ---------------------------------------------------------

VOLUME_DTYPE = "<i2"  # int16 little-endian
VOLUME_ITEMSIZE = 2

# Multipart part size. Sized by TIME, not by any size cap -- every PUT is well
# under Cloudflare's 100 MB request-body limit either way.
#
# This was 32 MiB, which broke real uploads. Traefik v3 defaults
# `respondingTimeouts.readTimeout` to 60 s and that clock covers reading the
# whole request BODY, so any single PUT slower than a minute is truncated
# mid-stream: SeaweedFS logs "unexpected EOF" and the client is told 499. A
# 32 MiB part needs ~150 s on a ~2 Mbit/s clinical uplink, so it never had a
# chance. The cluster's readTimeout is now 900 s, but 8 MiB parts are what make
# this robust in general: hospital proxies and Cloudflare enforce their own
# timeouts that we do not control, and a short request is the only portable
# defence. It also buys finer upload progress and cheaper retries.
#
# 5 MiB is deliberately the S3 floor (== MIN_PART_SIZE): the smallest part the
# protocol allows is also the most timeout-robust, which leaves no knob left to
# get wrong. 8 MiB was tried first and failed the budget in
# tests/test_wire_part_size.py -- it needs ~34 s at the uplink actually measured
# on the Eclipse workstation, leaving too little margin under a 60 s timeout.
#
# Do not raise this without re-reading that test.
PART_SIZE = 5 * 1024 * 1024
# S3 minimum for any part that is not the last one.
MIN_PART_SIZE = 5 * 1024 * 1024
# S3 hard limit on parts per upload; also bounds how many URLs we presign.
MAX_PARTS = 1000


def part_count(total_bytes: int) -> int:
    """Number of ``PART_SIZE`` parts needed for ``total_bytes`` (at least 1)."""
    if total_bytes <= 0:
        raise ValueError("total_bytes must be positive")
    return max(1, -(-total_bytes // PART_SIZE))  # ceil division


# --- volume identity -------------------------------------------------------

# The fields that make two uploads the *same* volume. Ordered and explicit rather
# than "whatever the geometry dict happens to contain", so adding a field to
# Geometry cannot silently change every existing volume's identity and invalidate
# the whole cache.
GEOMETRY_IDENTITY_FIELDS = (
    "x_size", "y_size", "z_size",
    "x_res", "y_res", "z_res",
    "origin", "row_direction", "col_direction", "slice_direction",
    "scaling_slope", "scaling_intercept",
)


def geometry_sha256(geometry: Mapping[str, Any]) -> str:
    """Digest of the geometry, for the volume dedup key.

    Content alone is not a safe key. The blob is ``gzip(int16 (Z,Y,X))`` with no
    header, so identical bytes pin the voxel *count* but not the shape —
    ``(512,512,100)`` and ``(256,1024,100)`` are indistinguishable — and the HU
    rescale does not appear in the bytes at all. Reusing a volume under the wrong
    geometry would place contours incorrectly in a patient, so geometry is part of
    the identity and that failure mode is structurally unreachable.

    Floats go through ``repr`` via the JSON encoder, which round-trips exactly for
    IEEE doubles, so the digest is stable for a given set of values without
    needing a tolerance. Two clients that compute the same geometry to the last
    bit agree; two that differ get separate volumes, which is the safe direction
    to fail.
    """
    canonical = {k: geometry[k] for k in GEOMETRY_IDENTITY_FIELDS if k in geometry}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- result download -------------------------------------------------------

RESULT_SCHEMA_VERSION = 2


def result_envelope(
    job_id: str,
    model: str,
    prompts: Iterable[str],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """The JSON document stored at ``result.json.gz``.

    ``results`` is one entry per prompt, in prompt order::

        {"prompt": "liver",
         "voxel_count": 812345,
         "contours": [{"z_index": 42, "points_lps": [[x, y, z], ...]}, ...]}

    ``points_lps`` are millimetres in the DICOM patient coordinate system, ready
    for ``structure.AddContourOnImagePlane(points, z_index)`` in ESAPI.
    """
    return {
        "schema": RESULT_SCHEMA_VERSION,
        "job_id": job_id,
        "model": model,
        "prompts": list(prompts),
        "results": results,
    }

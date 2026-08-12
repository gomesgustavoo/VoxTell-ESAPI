"""Part size is a TIMEOUT budget, not a size budget.

This file exists because a 32 MiB ``PART_SIZE`` shipped and broke every real
upload. Traefik v3 defaults ``respondingTimeouts.readTimeout`` to 60 s, and that
clock covers reading the request *body*, so one slow PUT is cut mid-stream --
SeaweedFS logs ``unexpected EOF`` and the client sees 499. Nothing about the
part size looked wrong; only the wall clock did.

So the invariant worth pinning is not "parts are small" but "one part transfers
inside the tightest timeout we might meet, at the slowest uplink we support".
Cloudflare and hospital proxies impose their own limits that we cannot raise.
"""

from __future__ import annotations

from api.config import settings
from voxtell_cloud.wire import MAX_PARTS, MIN_PART_SIZE, PART_SIZE, part_count

# The tightest read timeout we assume any intermediary might impose. Traefik's
# own v3 default, which is exactly what bit us.
TIGHTEST_TIMEOUT_SECONDS = 60
# A slow clinical uplink. 2 Mbit/s = 250 kB/s; radiotherapy departments are not
# always on the fast side of the hospital network.
SLOW_UPLINK_BYTES_PER_SECOND = 250 * 1000
# Leave headroom for TLS, the request itself and network jitter rather than
# sizing to exactly 100 % of the budget.
SAFETY_FACTOR = 0.5


def test_one_part_uploads_inside_the_tightest_timeout_on_a_slow_uplink():
    seconds = PART_SIZE / SLOW_UPLINK_BYTES_PER_SECOND
    budget = TIGHTEST_TIMEOUT_SECONDS * SAFETY_FACTOR
    assert seconds <= budget, (
        f"a {PART_SIZE / 1024 / 1024:.0f} MiB part needs ~{seconds:.0f}s at "
        f"{SLOW_UPLINK_BYTES_PER_SECOND / 1000:.0f} kB/s, over the {budget:.0f}s "
        "budget -- this is the 499/unexpected-EOF bug returning"
    )


def test_part_size_still_satisfies_the_s3_minimum():
    # Every part but the last must be at least 5 MiB or the multipart completes
    # with an error, so the timeout budget above cannot be met by shrinking
    # without limit.
    assert PART_SIZE >= MIN_PART_SIZE


def test_the_largest_allowed_upload_fits_in_the_part_limit():
    parts = part_count(settings.VOXTELL_MAX_UPLOAD_BYTES)
    assert parts <= MAX_PARTS, (
        f"{parts} parts for a {settings.VOXTELL_MAX_UPLOAD_BYTES} byte upload "
        f"exceeds the S3 limit of {MAX_PARTS}"
    )


def test_a_real_ct_splits_into_several_parts():
    # The volume that failed in Eclipse: 35,788,947 bytes gzipped. It must not
    # collapse back to one long-lived request.
    parts = part_count(35_788_947)
    assert parts > 1
    assert parts == 7

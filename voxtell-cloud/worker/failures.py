"""Is this failure worth retrying?

Before this, every exception was terminal: ``finish_failure`` and done. That is wrong
in both directions — a blip reaching SeaweedFS threw away a job the user had already
uploaded and paid a quota unit for, while a genuinely broken input was reported with no
signal distinguishing it from infrastructure trouble.

TWO DESIGN CONSTRAINTS, BOTH DELIBERATE
---------------------------------------
**This module must not import torch.** It is the one piece of failure handling that
wants unit tests, and importing torch to get ``torch.cuda.OutOfMemoryError`` would drag
CUDA into the test environment. OOM is therefore matched **by class name**, which is
also more robust: upstream, numpy, and torch each raise their own differently-typed
OOM, and every one of them is spelled with "OutOfMemory" or "CUDA out of memory".

**Unknown means permanent.** The tempting default is the opposite — retry what you do
not understand. But there is one GPU shared with another product, so retrying an
unclassified failure three times spends three times the GPU and delays every other
tenant, to rediscover the same failure. Failing fast surfaces it to the user, who can
resubmit deliberately, and ``jobs.failure_class`` records the verdict so the metric
tells you when this classifier is guessing wrong.
"""

from __future__ import annotations

import errno
import logging
from typing import Literal

log = logging.getLogger("worker.failures")

FailureClass = Literal["transient", "permanent"]

# Exception class names that mean "the environment misbehaved, try again". Matched by
# name so this module stays free of botocore/psycopg/torch imports.
_TRANSIENT_NAMES = frozenset({
    # botocore / urllib3 transport
    "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
    "ConnectionClosedError", "IncompleteReadError", "ResponseStreamingError",
    "HTTPClientError", "ConnectionError", "NewConnectionError", "ProtocolError",
    # database
    "OperationalError", "InterfaceError", "AdminShutdown", "CannotConnectNow",
    # ours — raised when the GPU-mutex database is unreachable
    "Transient",
    # memory pressure: worth exactly one more attempt, because upstream's own
    # VRAM-halving fallback may succeed once another tenant releases the card.
    "OutOfMemoryError", "CudaOutOfMemoryError", "MemoryError",
})

# S3 error codes that are retryable. Everything else from S3 (NoSuchKey, AccessDenied,
# InvalidArgument...) describes a request that will fail identically forever.
_TRANSIENT_S3_CODES = frozenset({
    "InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout",
    "RequestTimeTooSkewed", "ThrottlingException", "TooManyRequests",
    "500", "502", "503", "504",
})

# Substrings that identify a transient condition in a message when the type does not.
_TRANSIENT_SUBSTRINGS = (
    "cuda out of memory",
    "connection reset",
    "connection refused",
    "temporary failure in name resolution",
    "server closed the connection unexpectedly",
    "could not connect to server",
    "no space left on device",
)


def _s3_error_code(exc: BaseException) -> str | None:
    """The S3/botocore error code, if this is a ClientError-shaped exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = error.get("Code")
        if code:
            return str(code)
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status:
            return str(status)
    return None


def classify(exc: BaseException) -> FailureClass:
    """"transient" (requeue with backoff) or "permanent" (fail now)."""
    name = type(exc).__name__
    message = str(exc).lower()

    # Disk exhaustion is transient in the useful sense: the scratch emptyDir may be
    # full because a *neighbouring* job is mid-flight, and will not be in a minute.
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EIO, errno.EAGAIN):
        return "transient"

    code = _s3_error_code(exc)
    if code is not None:
        # A ClientError is only retryable for a specific set of codes. NoSuchKey on
        # the input volume in particular is permanent — the object is gone, and no
        # amount of waiting brings it back.
        return "transient" if code in _TRANSIENT_S3_CODES else "permanent"

    if name in _TRANSIENT_NAMES:
        return "transient"

    if any(fragment in message for fragment in _TRANSIENT_SUBSTRINGS):
        return "transient"

    # Everything else, including the content-hash mismatch from
    # voxtell_cloud.geometry.decode_volume, geometry/shape errors, and anything
    # unrecognised. See the module docstring for why unknown is permanent.
    return "permanent"


def backoff_seconds(attempts: int) -> float:
    """Delay before a requeued job becomes eligible again.

    Exponential from 30 s, capped at 10 minutes. The cap matters more than the curve:
    with ``WORKER_MAX_ATTEMPTS = 3`` the total added delay is bounded at a few minutes,
    which is short next to a user's patience and long enough for a SeaweedFS restart
    or another tenant's job to finish.
    """
    return float(min(30 * (2 ** max(0, attempts)), 600))

"""Which failures are worth retrying.

Pure unit tests, no database — and deliberately no torch. ``worker/failures.py`` matches
OOM **by class name** precisely so this file can assert the behaviour without dragging
CUDA into the test environment, and these tests are what keep that property honest:
they construct a fake exception class named ``OutOfMemoryError`` and expect it to be
recognised.

The default matters most. Before this module every exception was terminal, which threw
away a job over a SeaweedFS blip the user had already uploaded and paid a quota unit
for. But the opposite default is worse here: with one GPU shared with another product,
retrying an unclassified failure three times spends three times the GPU to rediscover
the same problem. So unknown means permanent, and
``test_unknown_exception_is_permanent`` states that as an intentional choice rather than
an omission.
"""

from __future__ import annotations

import errno

import pytest

from worker import failures
from worker.settings import settings


def _client_error(code: str, status: int | None = None) -> Exception:
    """A botocore ClientError-shaped exception, without importing botocore."""

    class ClientError(Exception):
        pass

    exc = ClientError(f"An error occurred ({code})")
    response: dict = {"Error": {"Code": code}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    exc.response = response  # type: ignore[attr-defined]
    return exc


def _named(name: str, message: str = "boom") -> Exception:
    return type(name, (Exception,), {})(message)


# ------------------------------------------------------------------- transient


@pytest.mark.parametrize(
    "name",
    [
        "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
        "ConnectionClosedError", "OperationalError", "InterfaceError",
        "Transient", "MemoryError",
    ],
)
def test_transport_and_database_errors_are_transient(name: str) -> None:
    assert failures.classify(_named(name)) == "transient"


def test_oom_is_matched_by_name_not_by_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason this module never imports torch.

    torch, numpy and upstream each raise their own differently-typed OOM. Matching on
    the name catches all of them and keeps the classifier importable on a CPU-only box.
    """
    assert failures.classify(_named("OutOfMemoryError")) == "transient"
    assert failures.classify(_named("CudaOutOfMemoryError")) == "transient"
    # And by message, for the wrappers that re-raise as RuntimeError.
    assert failures.classify(RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")) == (
        "transient"
    )


@pytest.mark.parametrize(
    "code", ["InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout", "503"]
)
def test_retryable_s3_codes_are_transient(code: str) -> None:
    assert failures.classify(_client_error(code)) == "transient"


def test_enospc_is_transient() -> None:
    """The scratch emptyDir may be full because a NEIGHBOURING job is mid-flight."""
    exc = OSError(errno.ENOSPC, "No space left on device")
    assert failures.classify(exc) == "transient"


def test_gpu_lock_unreachable_is_transient() -> None:
    """An unreachable mutex database must slow the queue, not empty it.

    Before this, ``gpu_lock()`` raised something generic when the ``gpulock`` database
    was down and every job failed **permanently** — losing work because a lock server
    blinked.
    """
    from worker.gpu_lock import Transient

    assert failures.classify(Transient("gpulock down")) == "transient"


# ------------------------------------------------------------------- permanent


@pytest.mark.parametrize("code", ["NoSuchKey", "AccessDenied", "InvalidArgument", "404", "403"])
def test_non_retryable_s3_codes_are_permanent(code: str) -> None:
    """NoSuchKey on the input volume in particular: no amount of waiting helps."""
    assert failures.classify(_client_error(code)) == "permanent"


def test_content_hash_mismatch_is_permanent() -> None:
    """The exact failure seen in production, and it must never be retried.

    A mismatch means the uploaded bytes are not what the client declared. Retrying
    re-runs the same broken input; worse, treating it as flaky would blur a check whose
    whole purpose is patient safety (a wrong hash could serve another series' contours).
    """
    exc = ValueError(
        "uploaded volume does not match its declared content hash "
        "(expected 0000000000000000, got e1cf3da75a388f2a) — the series was not stored "
        "correctly and must be uploaded again"
    )
    assert failures.classify(exc) == "permanent"


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("affine_lps"),
        ValueError("shape mismatch"),
        AssertionError("invariant broken"),
        TypeError("not indexable"),
    ],
)
def test_input_and_programming_errors_are_permanent(exc: Exception) -> None:
    assert failures.classify(exc) == "permanent"


def test_unknown_exception_is_permanent() -> None:
    """An intentional choice, not an omission — see the module docstring."""
    assert failures.classify(_named("SomethingNobodyAnticipated")) == "permanent"


# --------------------------------------------------------------------- backoff


def test_backoff_is_monotonic_and_capped() -> None:
    delays = [failures.backoff_seconds(n) for n in range(0, 12)]
    assert delays == sorted(delays), "backoff must never shrink as attempts grow"
    assert max(delays) <= 600, "backoff must be capped so a retry is not effectively lost"
    assert delays[0] >= 30, "the first retry should not be immediate"


def test_total_backoff_stays_within_a_users_patience() -> None:
    """With WORKER_MAX_ATTEMPTS attempts the added delay must stay in minutes.

    Long enough for a SeaweedFS restart or another tenant's job to finish; short enough
    that a planner does not conclude the job is lost.
    """
    total = sum(failures.backoff_seconds(n) for n in range(settings.WORKER_MAX_ATTEMPTS))
    assert total <= 15 * 60, f"cumulative backoff of {total}s is too long to wait out"

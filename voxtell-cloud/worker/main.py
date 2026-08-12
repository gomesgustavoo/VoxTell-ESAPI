"""GPU worker entrypoint: an async pipeline over the jobs queue.

One GPU means one inference at a time, and the single-thread GPU executor *is*
that serialization point. Preprocess and postprocess run in a process pool so a
neighbouring job's CPU work overlaps the one in flight — which is the whole
throughput argument for a queue on a single-GPU box:

    GPU:      [   infer N   ][  infer N+1  ]
    CPU-pre:     [prep N+1]
    CPU-post:                 [contours N]

A semaphore caps total in-flight jobs (backpressure), and one heartbeat
coroutine keeps every in-flight job's ``heartbeat_at`` fresh plus touches the
liveness file — so a pod death is self-healing via the stale sweep, exactly like
the CPU worker that already runs on this cluster.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing as mp
import os
import shutil
import signal
import socket
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from . import failures, gpu_lock, health, job, metrics, s3, stages, watchdog
from .settings import settings

log = logging.getLogger("worker.main")


@contextlib.contextmanager
def _stage(name: str):
    """Time one pipeline stage into voxtell_stage_seconds{stage=...}.

    Answers "is the bottleneck upload, preprocess, the GPU or postprocess?", which was
    previously unanswerable — measured GPU time is 0.4-76 s while whole jobs take much
    longer, so the GPU is almost certainly not where the time goes.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        metrics.STAGE_SECONDS.labels(stage=name).observe(time.monotonic() - started)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("botocore", "boto3", "urllib3", "nnunetv2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _amain() -> None:
    # Imported lazily so an import error in the ML stack surfaces with logging
    # configured rather than as a bare traceback at interpreter start.
    from . import engine

    loop = asyncio.get_running_loop()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"[:64]
    log.info(
        "voxtell worker %s starting (cpu_conc=%d, poll=%ss, stale=%dmin)",
        worker_id, settings.INFER_CPU_CONCURRENCY,
        settings.WORKER_POLL_SECONDS, settings.WORKER_HEARTBEAT_STALE_MINUTES,
    )

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    metrics.serve(
        settings.WORKER_METRICS_PORT,
        version=settings.INFER_MODEL_DIR.rsplit("/", 1)[-1],
        model=settings.INFER_MODEL_DIR,
    )

    # A False return is no longer ignored: gpu_lock() now raises Transient when the
    # mutex database is unreachable, so jobs are retried with backoff instead of every
    # one of them failing permanently.
    gpu_lock.check()

    cpu_pool = ProcessPoolExecutor(
        max_workers=settings.INFER_CPU_CONCURRENCY,
        mp_context=mp.get_context("spawn"),
    )
    gpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu")

    if settings.INFER_WARM_ON_START:
        await loop.run_in_executor(gpu_pool, engine.warm_up)
    health.touch()

    inflight: set = set()
    # One more than the CPU pool: one job on the GPU while the pool does pre/post
    # for its neighbours.
    sem = asyncio.Semaphore(settings.INFER_CPU_CONCURRENCY + 1)

    leases = watchdog.LeaseRegistry()

    async def lease_loop() -> None:
        """Renew leases for progressing jobs, and assert process liveness — both
        CONDITIONALLY.

        This replaced an unconditional heartbeat, and the conditionality is the entire
        fix for the cross-product deadlock. Previously this loop wrote heartbeat_at and
        touched /tmp/alive on a timer, which keeps ticking while the GPU thread is
        wedged inside a CUDA call. A wedged job therefore heartbeated forever, kept the
        liveness probe green forever, and held the Postgres advisory GPU lock forever —
        blocking DicomSegVR's inference pod in another namespace with no alert.

        Now: a stalled job's lease is left to expire (so the API's reclaim loop
        recovers it), and — the load-bearing part — ``health.touch()`` is skipped while
        ANY in-flight job is stalled. /tmp/alive goes stale, the existing exec probe
        kills the pod, the lock connection dies, Postgres releases the session-level
        advisory lock automatically, and DicomSegVR resumes. No new machinery; it just
        stops lying to the probe that is already there.
        """
        while not stop.is_set():
            for jid, lease in leases.items():
                phase = lease.note_stall_once()
                if phase is not None:
                    metrics.STALLS.labels(phase=phase).inc()
                if lease.is_stalled():
                    metrics.LEASE_RENEWALS.labels(result="withheld").inc()
                    continue
                try:
                    await loop.run_in_executor(None, job.renew_lease, jid)
                    metrics.LEASE_RENEWALS.labels(result="renewed").inc()
                except Exception as exc:  # never let this kill the loop
                    log.warning("lease renewal failed for %s: %s", jid, exc)

            if leases.any_stalled():
                log.error(
                    "NOT touching the liveness file: a job is stalled. This pod should "
                    "be restarted so the GPU mutex is released."
                )
            else:
                health.touch()
            await _sleep_or_stop(stop, settings.WORKER_HEARTBEAT_SECONDS)

    async def process(claimed: job.ClaimedJob) -> None:
        jid = claimed.job_id
        lease = leases.add(jid, watchdog.PHASE_IO)
        reporter = job.ProgressReporter(jid, lease=lease)
        inflight.add(jid)
        metrics.INFLIGHT.set(len(inflight))
        prepared = None
        job_started = time.monotonic()
        outcome = "failed_permanent"
        try:
            cancelled = job.CancelWatcher(jid)

            if job.cancel_requested(jid):
                await loop.run_in_executor(None, job.finish_cancelled, jid)
                outcome = "cancelled"
                return

            await loop.run_in_executor(None, reporter, 0.05, "Fetching volume")
            lease.phase(watchdog.PHASE_IO)
            with _stage("preprocess"):
                prepared = await loop.run_in_executor(
                    cpu_pool, stages.preprocess, str(jid), claimed.volume_key, claimed.geometry
                )

            # Cancel check #1, and the highest-value one: BEFORE taking the GPU lock.
            # A cancel arriving during a 30 s download used to still run the whole GPU
            # job, spending a shared card on work the user had already abandoned.
            if cancelled():
                await loop.run_in_executor(None, job.finish_cancelled, jid)
                outcome = "cancelled"
                return

            await loop.run_in_executor(None, reporter, 0.20, "Preparing inference")
            masks_path = f"{prepared.scratch_dir}/masks.npy"
            lease.phase(watchdog.PHASE_COMPUTE)
            with _stage("gpu"):
                gpu_seconds = await loop.run_in_executor(
                    gpu_pool,
                    engine.segment,
                    prepared.data_path,
                    claimed.prompts,
                    claimed.keep_largest,
                    masks_path,
                    reporter,
                    cancelled,
                    lease,
                )
            metrics.GPU_SECONDS.inc(gpu_seconds)

            # Cancel check #2 — THE LIVE BUG. A cancel that landed after segment()
            # returned normally was never re-read, so the job was written as `done`,
            # a result was uploaded, and a UsageEvent was charged, for work the user
            # explicitly cancelled. Upstream only raises InferenceCancelled if the
            # cancel arrives while it is still iterating patches.
            if cancelled():
                log.info("job %s was cancelled just as inference finished", jid)
                await loop.run_in_executor(None, job.finish_cancelled, jid)
                outcome = "cancelled"
                if claimed.owns_volume:
                    await loop.run_in_executor(None, _safe_delete, claimed.volume_key)
                return

            await loop.run_in_executor(None, reporter, 0.72, "Extracting contours")
            lease.phase(watchdog.PHASE_IO)
            prefix = f"u/{claimed.user_id}/jobs/{jid}/"
            result_key = prefix + "result.json.gz"
            mask_key = prefix + "mask.bin.gz" if claimed.want_mask else None
            with _stage("postprocess"):
                summary = await loop.run_in_executor(
                    cpu_pool,
                    stages.postprocess,
                    str(jid),
                    prepared,
                    masks_path,
                    claimed.prompts,
                    claimed.affine_lps,
                    result_key,
                    mask_key,
                )

            # Cancel check #3. The result objects already exist by now, so unlike the
            # earlier checks this one cannot avoid the work — but it still keeps the
            # job's recorded state honest and leaves no result_key for the user to
            # download. Retention purges the orphaned objects on the normal TTL.
            if cancelled():
                await loop.run_in_executor(None, job.finish_cancelled, jid)
                outcome = "cancelled"
                if claimed.owns_volume:
                    await loop.run_in_executor(None, _safe_delete, claimed.volume_key)
                return

            await loop.run_in_executor(
                None,
                lambda: job.finish_success(
                    jid,
                    result_key=result_key,
                    mask_key=mask_key,
                    gpu_seconds=gpu_seconds,
                    message=(
                        f"Segmented {len(claimed.prompts)} structure(s), "
                        f"{summary['contours']} contour(s)"
                    ),
                ),
            )
            # The CT itself is dead weight (and PHI) the moment masks exist — but
            # only if this job owns it. A shared volume from POST /v1/volumes
            # belongs to the user and outlives this job on purpose, so that the
            # next prompt they try needs no re-upload; deleting it here would 404
            # the very next job and defeat the whole feature. The API's sweeper
            # retires it on its own TTL instead.
            if claimed.owns_volume:
                await loop.run_in_executor(None, _safe_delete, claimed.volume_key)
            outcome = "done"
            log.info("job %s done in %.1fs GPU", jid, gpu_seconds)

        except engine.Cancelled:
            log.info("job %s cancelled", jid)
            await loop.run_in_executor(None, job.finish_cancelled, jid)
            outcome = "cancelled"
            # Cancelling one prompt must not throw away the upload — that is a
            # feature, not an oversight: fix the prompt and go again.
            if claimed.owns_volume:
                await loop.run_in_executor(None, _safe_delete, claimed.volume_key)
        except Exception as exc:
            # Classify before deciding. Everything used to be terminal, which threw
            # away a job over a SeaweedFS blip the user had already uploaded and paid a
            # quota unit for. See worker/failures.py for why unknown means permanent.
            kind = failures.classify(exc)
            outcome = f"failed_{kind}"
            log.error(
                "job %s failed (%s): %s\n%s", jid, kind, exc, traceback.format_exc()
            )
            try:
                if kind == "transient":
                    delay = failures.backoff_seconds(claimed.attempts)
                    await loop.run_in_executor(
                        None, job.finish_transient_failure, jid, str(exc), delay
                    )
                else:
                    await loop.run_in_executor(
                        None, job.finish_failure, jid, str(exc), "permanent"
                    )
            except Exception as exc2:
                log.error("could not record failure for %s: %s", jid, exc2)
        finally:
            metrics.JOB_DURATION.labels(outcome=outcome).observe(time.monotonic() - job_started)
            leases.remove(jid)
            inflight.discard(jid)
            metrics.INFLIGHT.set(len(inflight))
            if prepared is not None:
                shutil.rmtree(prepared.scratch_dir, ignore_errors=True)
            metrics.observe_scratch(settings.WORKER_SCRATCH_DIR)
            sem.release()

    hb = asyncio.create_task(lease_loop())
    tasks: set = set()
    last_sweep = 0.0
    try:
        while not stop.is_set():
            await sem.acquire()
            if stop.is_set():
                sem.release()
                break
            try:
                # Throttled: this used to run on every iteration, i.e. twice per ~5 s,
                # which is ~34k pointless write transactions a day. The API's reclaim
                # loop is the primary path at 30 s; this is the fallback for when the
                # API is the thing that is down.
                now = time.monotonic()
                if now - last_sweep >= settings.WORKER_SWEEP_INTERVAL_SECONDS:
                    last_sweep = now
                    await loop.run_in_executor(None, job.sweep_stale)
                claimed = await loop.run_in_executor(None, job.claim_next, worker_id)
            except Exception as exc:
                log.error("queue poll failed (database down?): %s", exc)
                sem.release()
                await _sleep_or_stop(stop, min(60.0, settings.WORKER_POLL_SECONDS * 4))
                continue

            if claimed is None:
                sem.release()
                await _sleep_or_stop(stop, settings.WORKER_POLL_SECONDS)
                continue

            t = asyncio.create_task(process(claimed))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    finally:
        log.info("draining %d in-flight job(s)", len(tasks))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        hb.cancel()
        cpu_pool.shutdown(wait=False, cancel_futures=True)
        gpu_pool.shutdown(wait=False)
        log.info("voxtell worker %s stopped", worker_id)


def _safe_delete(key: str) -> None:
    try:
        s3.delete(key)
    except Exception as exc:
        log.warning("could not delete %s: %s", key, exc)


def main() -> None:
    _setup_logging()
    asyncio.run(_amain())


if __name__ == "__main__":
    main()

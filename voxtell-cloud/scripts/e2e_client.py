#!/usr/bin/env python3
"""End-to-end client that stands in for the Eclipse plugin.

Drives the whole v2 protocol exactly as the C# side will — create, presigned
multipart upload, submit, poll, download, verify — so the backend can be
exercised before the plugin exists.

    # against a real study
    python scripts/e2e_client.py --base https://voxtell.dicomsegvr.com/v1 \
        --key vxt_... --dicom ~/study/ --prompt brain --prompt cerebellum

    # against a phantom, no DICOM needed (checks plumbing, not anatomy)
    python scripts/e2e_client.py --base http://127.0.0.1:8000/v1 --key vxt_... \
        --synthetic --prompt liver

With --dicom the geometry is read from the series headers, so the LPS contours
that come back can be checked against the source grid: every returned point is
projected back to a voxel index and must land inside the volume, on the slice it
claims, and (for the phantom) inside the structure.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import requests


# --------------------------------------------------------------------------- #
# Building a volume + geometry
# --------------------------------------------------------------------------- #
def load_dicom_series(folder: Path) -> tuple[np.ndarray, dict]:
    """Read a DICOM series into ((Z, Y, X) int16, geometry dict).

    Slices are ordered along the true slice normal (the cross product of the
    row/column direction cosines), not by InstanceNumber — the latter lies often
    enough that sorting by it silently flips volumes.
    """
    import pydicom

    files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
    slices = []
    for path in files:
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=False)
        except Exception:
            continue
        if not hasattr(ds, "PixelData") or not hasattr(ds, "ImagePositionPatient"):
            continue
        slices.append(ds)
    if not slices:
        raise SystemExit(f"no readable image slices in {folder}")

    first = slices[0]
    orient = [float(v) for v in first.ImageOrientationPatient]
    row_dir, col_dir = np.array(orient[:3]), np.array(orient[3:])
    normal = np.cross(row_dir, col_dir)

    slices.sort(key=lambda ds: float(np.dot(np.array(ds.ImagePositionPatient, float), normal)))

    volume = np.stack([s.pixel_array.astype(np.int16) for s in slices])  # (Z, Y, X)
    z_size, y_size, x_size = volume.shape

    py, px = (float(v) for v in first.PixelSpacing)  # DICOM order is (row, col)
    if len(slices) > 1:
        p0 = np.array(slices[0].ImagePositionPatient, float)
        p1 = np.array(slices[1].ImagePositionPatient, float)
        z_res = float(abs(np.dot(p1 - p0, normal))) or float(
            getattr(first, "SliceThickness", 1.0)
        )
    else:
        z_res = float(getattr(first, "SliceThickness", 1.0))

    geometry = {
        "x_size": x_size, "y_size": y_size, "z_size": z_size,
        "x_res": px, "y_res": py, "z_res": z_res,
        "origin": [float(v) for v in slices[0].ImagePositionPatient],
        "row_direction": row_dir.tolist(),
        "col_direction": col_dir.tolist(),
        "slice_direction": normal.tolist(),
        "scaling_slope": float(getattr(first, "RescaleSlope", 1.0)),
        "scaling_intercept": float(getattr(first, "RescaleIntercept", 0.0)),
    }
    print(f"loaded {z_size} slices, {x_size}x{y_size}, spacing "
          f"{px:.3f}x{py:.3f}x{z_res:.3f} mm, modality {getattr(first,'Modality','?')}")
    return volume, geometry


def synthetic_volume(size: str | None = None, noise: int = 0) -> tuple[np.ndarray, dict]:
    """A phantom: air background with a bright sphere. Plumbing only.

    ``size`` is ``XxYxZ``; ``noise`` adds uniform +/-N HU of per-voxel noise.

    Noise exists to make the blob *incompressible*. The default phantom is
    piecewise-constant and gzips about 1000:1, so it is always a single upload
    part -- which is exactly why the 32 MiB part-size bug survived an
    end-to-end run and only surfaced on a real CT. Real CT has noise and
    compresses ~1.3:1, so `--noise 40 --size 256x256x300` reproduces a
    multi-part upload faithfully.
    """
    if size:
        try:
            x, y, z = (int(v) for v in size.lower().split("x"))
        except ValueError:
            raise SystemExit(f"--size must look like 256x256x300, got {size!r}")
    else:
        x, y, z = 128, 128, 48

    gz, gy, gx = np.ogrid[:z, :y, :x]
    d2 = (
        ((gz - z / 2) * 2.5) ** 2
        + ((gy - y / 2) * 1.0) ** 2
        + ((gx - x / 2) * 1.0) ** 2
    )
    radius = min(x, y) * 0.23
    volume = np.where(d2 <= radius**2, 60, -1000).astype(np.int16)

    if noise:
        rng = np.random.default_rng(20260805)
        volume = (
            volume.astype(np.int32)
            + rng.integers(-noise, noise + 1, size=volume.shape, dtype=np.int32)
        ).astype(np.int16)

    geometry = {
        "x_size": x, "y_size": y, "z_size": z,
        "x_res": 1.0, "y_res": 1.0, "z_res": 2.5,
        "origin": [-x / 2.0, -y / 2.0, -z * 2.5 / 2.0],
        "row_direction": [1.0, 0.0, 0.0],
        "col_direction": [0.0, 1.0, 0.0],
        "slice_direction": [0.0, 0.0, 1.0],
        "scaling_slope": 1.0,
        "scaling_intercept": 0.0,
    }
    print(f"synthetic phantom {x}x{y}x{z}"
          + (f", noise +/-{noise} HU" if noise else ""))
    return volume, geometry


def affine_lps(geom: dict) -> np.ndarray:
    aff = np.eye(4)
    aff[:3, 0] = np.array(geom["row_direction"]) * geom["x_res"]
    aff[:3, 1] = np.array(geom["col_direction"]) * geom["y_res"]
    aff[:3, 2] = np.array(geom["slice_direction"]) * geom["z_res"]
    aff[:3, 3] = geom["origin"]
    return aff


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
def make_session(key: str | None) -> requests.Session:
    """A session that survives a transient drop.

    A job runs for minutes and is polled throughout, so the connection WILL be
    reset occasionally — an idle keep-alive through a proxy, a load balancer
    recycling, a pod rollout. Losing the poll must not lose the job, so retries
    are built in here; the real ESAPI client needs the same tolerance.
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    if key:
        session.headers["Authorization"] = f"Bearer {key}"
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        # 502/503/504 plus Cloudflare's own 52x/530 family: results are fetched
        # from the object store through the CF edge, and a tunnel hiccup there
        # surfaces as 530 rather than a normal gateway error. Seen in testing.
        status_forcelist=(502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530),
        allowed_methods=frozenset(["GET", "PUT", "POST", "DELETE"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def upload_parts(uploads, upload_list: list[dict], blob: bytes, part_size: int,
                 label: str = "part") -> list[dict]:
    """PUT each presigned part and collect its ETag. Returns bytes actually sent."""
    parts = []
    for part in upload_list:
        n = part["part_number"]
        chunk = blob[(n - 1) * part_size : n * part_size]
        # No Authorization header on a presigned URL — adding one breaks the
        # signature. A bare session gives the retry policy without the header.
        put = uploads.put(part["url"], data=chunk, timeout=600)
        put.raise_for_status()
        parts.append({"part_number": n, "etag": put.headers["ETag"]})
        print(f"  {label} {n}/{len(upload_list)} ({len(chunk) / 1e6:.1f} MB) ok")
    return parts


def poll_until_terminal(session, base: str, job_id: str, timeout: float) -> dict | None:
    """Print progress until the job is terminal. None means the wait timed out."""
    last = ""
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        status = session.get(f"{base}/jobs/{job_id}", timeout=30).json()
        line = f"  [{status['state']}] {status['progress'] * 100:5.1f}%  {status['message'] or ''}"
        if line != last:
            print(line, flush=True)
            last = line
        if status["state"] in ("done", "failed", "cancelled", "expired"):
            return status
        if deadline and time.monotonic() > deadline:
            print(f"still {status['state']} after {timeout:.0f}s — giving up on "
                  f"waiting (the job keeps running; poll {base}/jobs/{job_id})")
            return None
        time.sleep(status.get("poll_after", 5))


def run_volume_mode(args, session, base, volume, geometry, blob) -> int:
    """Upload the series once via POST /v1/volumes, then run N jobs against it.

    This is the whole point of the v3 protocol, so the assertions are about *bytes
    not moved*, not merely about the jobs succeeding. A version of this feature that
    quietly re-uploaded every time would pass a naive end-to-end test.
    """
    content_sha256 = hashlib.sha256(volume.astype("<i2").tobytes(order="C")).hexdigest()
    print(f"content sha256 {content_sha256[:16]}…")

    uploads = make_session(key=None)

    def create_volume() -> dict:
        resp = session.post(
            f"{base}/volumes",
            json={
                "geometry": geometry,
                "upload_bytes": len(blob),
                "content_sha256": args.corrupt_hash and ("00" * 32) or content_sha256,
            },
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            print(f"volume create failed {resp.status_code}: {resp.text}", file=sys.stderr)
            sys.exit(1)
        return resp.json()

    t0 = time.monotonic()
    created = create_volume()
    vid, part_size = created["volume_id"], created["part_size"]
    print(f"volume {vid} state={created['state']} reused={created['reused']} "
          f"parts={len(created['upload'])} expires_at={created['expires_at']}")

    if created["upload"]:
        parts = upload_parts(uploads, created["upload"], blob, part_size, "vol part")
        resp = session.post(f"{base}/volumes/{vid}/complete", json={"parts": parts}, timeout=120)
        if resp.status_code != 200:
            print(f"complete failed {resp.status_code}: {resp.text}", file=sys.stderr)
            return 1
        print(f"volume ready after {time.monotonic() - t0:.1f}s")
    else:
        print("nothing to upload — the server already held this series")

    # --- the re-POST check: same hash must return the same id and no upload ---
    if args.reupload_after_complete:
        again = create_volume()
        ok = (again["volume_id"] == vid and again["reused"] and not again["upload"])
        print(f"re-POST same hash -> id_match={again['volume_id'] == vid} "
              f"reused={again['reused']} parts={len(again['upload'])} "
              f"{'OK' if ok else '*** FAIL ***'}")
        if not ok:
            return 1

    # --- the geometry-in-the-key check: same bytes, different HU mapping ---
    if args.geometry_tweak:
        tweaked = dict(geometry)
        tweaked["scaling_intercept"] = float(geometry.get("scaling_intercept", 0.0)) - 24.0
        resp = session.post(
            f"{base}/volumes",
            json={"geometry": tweaked, "upload_bytes": len(blob),
                  "content_sha256": content_sha256},
            timeout=60,
        )
        body = resp.json()
        distinct = body["volume_id"] != vid
        print(f"geometry tweak -> new volume={distinct} parts={len(body['upload'])} "
              f"{'OK' if distinct and body['upload'] else '*** FAIL ***'}")
        if not distinct:
            return 1
        session.delete(f"{base}/volumes/{body['volume_id']}", timeout=30)

    # --- run N jobs against the one volume ---
    runs = max(1, args.volume_runs)
    prompts = args.prompt
    timings, uploaded_after_first = [], 0
    for i in range(runs):
        prompt = [prompts[i % len(prompts)]]
        j0 = time.monotonic()
        resp = session.post(
            f"{base}/jobs",
            json={"volume_id": vid, "prompts": prompt,
                  "keep_largest": args.keep_largest, "want_mask": args.want_mask},
            timeout=60,
        )
        if resp.status_code != 201:
            print(f"job create failed {resp.status_code}: {resp.text}", file=sys.stderr)
            return 1
        job = resp.json()
        if job["upload"]:
            uploaded_after_first += len(job["upload"])
        print(f"run {i + 1}/{runs} prompt={prompt[0]!r} job={job['job_id']} "
              f"state={job['state']} upload_parts={len(job['upload'])}")

        status = poll_until_terminal(session, base, job["job_id"], args.timeout)
        if status is None:
            return 2
        if status["state"] != "done":
            print(f"run {i + 1} ended {status['state']}: {status.get('error')}", file=sys.stderr)
            if args.corrupt_hash:
                print("expected: --corrupt-hash must NOT segment")
                return 0
            return 1
        timings.append(time.monotonic() - j0)
        print(f"run {i + 1} done in {timings[-1]:.1f}s")

    if args.corrupt_hash:
        print("*** FAIL: a corrupt content hash was segmented instead of rejected")
        return 1

    # The feature, asserted. A job against a held volume must move zero bytes.
    print(f"\nuploaded parts across all {runs} job create(s): {uploaded_after_first} "
          f"{'OK' if uploaded_after_first == 0 else '*** FAIL ***'}")
    if uploaded_after_first:
        return 1
    if runs > 1:
        print(f"per-run wall clock: {', '.join(f'{t:.1f}s' for t in timings)}")

    vol = session.get(f"{base}/volumes/{vid}", timeout=30).json()
    print(f"volume jobs_run={vol['jobs_run']} state={vol['state']} expires_at={vol['expires_at']}")

    # --- the release lever, and that a job against a released volume 404s ---
    if args.release:
        resp = session.delete(f"{base}/volumes/{vid}", timeout=30)
        print(f"DELETE /volumes/{vid} -> {resp.status_code}")
        after = session.post(f"{base}/jobs", json={"volume_id": vid, "prompts": prompts[:1]},
                             timeout=60)
        ok = after.status_code == 404
        print(f"job against released volume -> {after.status_code} "
              f"{'OK' if ok else '*** FAIL ***'}")
        return 0 if ok else 1

    return 0


def run(args) -> int:
    volume, geometry = (
        synthetic_volume(args.size, args.noise) if args.synthetic
        else load_dicom_series(Path(args.dicom))
    )

    blob = gzip.compress(volume.astype("<i2").tobytes(order="C"), compresslevel=6)
    print(f"volume {volume.nbytes / 1e6:.1f} MB raw -> {len(blob) / 1e6:.1f} MB gzip")

    session = make_session(args.key)
    if args.host_header:
        # Before the public DNS name exists, the API is reachable by forwarding
        # Traefik and supplying the Host it routes on. Only the API calls need it;
        # the presigned part PUTs go straight to the object store.
        session.headers["Host"] = args.host_header
    base = args.base.rstrip("/")

    who = session.get(f"{base}/me", timeout=30)
    who.raise_for_status()
    me = who.json()
    caps = me.get("capabilities") or []
    print(f"authenticated as {me.get('email') or me['id']}  capabilities={caps or '[]'}")

    wants_volumes = (
        args.volume_runs or args.reupload_after_complete or args.release
        or args.corrupt_hash or args.geometry_tweak
    )
    if wants_volumes:
        if "volumes" not in caps:
            print("this deployment does not advertise the 'volumes' capability — "
                  "set VOXTELL_VOLUMES_ENABLED=true", file=sys.stderr)
            return 1
        return run_volume_mode(args, session, base, volume, geometry, blob)

    # 1. create
    t0 = time.monotonic()
    resp = session.post(
        f"{base}/jobs",
        json={
            "geometry": geometry,
            "prompts": args.prompt,
            "upload_bytes": len(blob),
            "keep_largest": args.keep_largest,
            "want_mask": args.want_mask,
        },
        timeout=60,
    )
    if resp.status_code != 201:
        print(f"create failed {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1
    created = resp.json()
    job_id, part_size = created["job_id"], created["part_size"]
    print(f"job {job_id} created, {len(created['upload'])} part(s)")

    # 2. upload the parts straight to object storage (no Authorization header —
    #    the presigned URL carries its own signature and adding one breaks it)
    uploads = make_session(key=None)
    parts = []
    for part in created["upload"]:
        n = part["part_number"]
        chunk = blob[(n - 1) * part_size : n * part_size]
        # No Authorization header on a presigned URL — adding one breaks the
        # signature. A bare session gives the retry policy without the header.
        put = uploads.put(part["url"], data=chunk, timeout=600)
        put.raise_for_status()
        parts.append({"part_number": n, "etag": put.headers["ETag"]})
        print(f"  part {n}/{len(created['upload'])} ({len(chunk) / 1e6:.1f} MB) ok")

    # 3. submit
    resp = session.post(f"{base}/jobs/{job_id}/submit", json={"parts": parts}, timeout=120)
    if resp.status_code != 200:
        print(f"submit failed {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1
    print(f"submitted after {time.monotonic() - t0:.1f}s, state={resp.json()['state']}")

    if args.cancel_after:
        time.sleep(args.cancel_after)
        resp = session.post(f"{base}/jobs/{job_id}/cancel", timeout=30)
        # Report the outcome: a cancel that silently failed to send looks exactly
        # like a cancel the server ignored, which is a confusing way to debug.
        body = resp.json() if resp.ok else resp.text
        state = body.get("state") if isinstance(body, dict) else body
        print(f"cancel requested after {args.cancel_after}s -> HTTP {resp.status_code}, state={state}")

    # 4. poll
    last = ""
    deadline = time.monotonic() + args.timeout if args.timeout else None
    while True:
        status = session.get(f"{base}/jobs/{job_id}", timeout=30).json()
        line = f"  [{status['state']}] {status['progress'] * 100:5.1f}%  {status['message'] or ''}"
        if line != last:
            print(line, flush=True)
            last = line
        if status["state"] in ("done", "failed", "cancelled", "expired"):
            break
        if deadline and time.monotonic() > deadline:
            print(f"still {status['state']} after {args.timeout:.0f}s — giving up on "
                  f"waiting (the job keeps running; poll {base}/jobs/{job_id})")
            return 2
        time.sleep(status.get("poll_after", 5))

    print(f"finished in {time.monotonic() - t0:.1f}s: {status['state']}")
    if status["state"] != "done":
        if status.get("error"):
            print(f"error: {status['error']}", file=sys.stderr)
        return 0 if status["state"] == "cancelled" else 1

    # 5. download + verify
    resp = session.get(f"{base}/jobs/{job_id}/result", timeout=300)
    resp.raise_for_status()
    payload = json.loads(gzip.decompress(resp.content))
    print(f"result {len(resp.content) / 1e6:.2f} MB gzip, model {payload['model']}")

    return verify(payload, geometry, volume, Path(args.save) if args.save else None)


def verify(payload: dict, geometry: dict, volume: np.ndarray, save: Path | None) -> int:
    """Project every contour point back to a voxel index and sanity-check it."""
    aff = affine_lps(geometry)
    inv = np.linalg.inv(aff)
    x_size, y_size, z_size = geometry["x_size"], geometry["y_size"], geometry["z_size"]
    problems = 0

    for result in payload["results"]:
        contours = result["contours"]
        n_points = sum(len(c["points_lps"]) for c in contours)
        if not contours:
            print(f"  {result['prompt']:<28} EMPTY — nothing segmented")
            continue

        pts = np.vstack([np.asarray(c["points_lps"]) for c in contours])
        vox = (inv @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]

        # Every point must be inside the grid...
        oob = (
            (vox[:, 0] < -1) | (vox[:, 0] > x_size)
            | (vox[:, 1] < -1) | (vox[:, 1] > y_size)
            | (vox[:, 2] < -1) | (vox[:, 2] > z_size)
        ).sum()
        # ...and each contour's points must sit on the slice it says they do.
        z_err = max(
            float(np.abs((inv @ np.c_[np.asarray(c["points_lps"]),
                                      np.ones(len(c["points_lps"]))].T).T[:, 2]
                         - c["z_index"]).max())
            for c in contours
        )
        z_span = f"{min(c['z_index'] for c in contours)}-{max(c['z_index'] for c in contours)}"

        status = "ok"
        if oob:
            status, problems = f"{oob} POINT(S) OUT OF BOUNDS", problems + 1
        elif z_err > 1e-6:
            status, problems = f"Z MISMATCH {z_err:.2e}", problems + 1

        print(
            f"  {result['prompt']:<28} {result['voxel_count']:>9,} vox  "
            f"{len(contours):>4} contours  {n_points:>7,} pts  z {z_span:<9} {status}"
        )

    if save:
        save.write_text(json.dumps(payload, indent=2))
        print(f"saved {save}")

    if problems:
        print(f"\n{problems} structure(s) failed geometric verification", file=sys.stderr)
        return 1
    print("\nall structures verified against the source grid")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", required=True, help="API base, e.g. https://voxtell.dicomsegvr.com/v1")
    p.add_argument("--key", required=True, help="vxt_... API key (or a Keycloak access token)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dicom", help="Directory holding a DICOM series")
    src.add_argument("--synthetic", action="store_true", help="Use a built-in phantom")
    p.add_argument("--size", help="Phantom dimensions XxYxZ (default 128x128x48)")
    p.add_argument("--noise", type=int, default=0,
                   help="Add +/-N HU of noise so the blob does not gzip to nothing; "
                        "needed to exercise MULTI-PART upload")
    p.add_argument("--prompt", action="append", required=True, help="Repeatable")
    p.add_argument("--keep-largest", action="store_true")
    p.add_argument("--want-mask", action="store_true")
    p.add_argument("--cancel-after", type=float, default=0,
                   help="Seconds after submit to request cancellation (cancel test)")
    p.add_argument("--host-header",
                   help="Host header for the API calls (e.g. voxtell.dicomsegvr.com) "
                        "when reaching Traefik directly before DNS exists")
    p.add_argument("--timeout", type=float, default=0,
                   help="Stop waiting after N seconds and exit 2. The job is unaffected.")
    p.add_argument("--save", help="Write the decoded result JSON here")

    vol = p.add_argument_group(
        "reusable volumes (v3)",
        "Any of these switches the client to POST /v1/volumes. Requires the "
        "'volumes' capability on /v1/me.",
    )
    vol.add_argument("--volume-runs", type=int, default=0, metavar="N",
                     help="Upload once, then run N jobs against the same volume. "
                          "Cycles through --prompt. Asserts runs move zero bytes.")
    vol.add_argument("--reupload-after-complete", action="store_true",
                     help="Re-POST the same content hash; expects the same volume_id, "
                          "reused=true and an empty upload list (the 're-opened the "
                          "plugin on the same patient' case)")
    vol.add_argument("--geometry-tweak", action="store_true",
                     help="POST the same bytes with a different scaling_intercept; "
                          "expects a DISTINCT volume, because reusing across geometries "
                          "would place contours wrongly in a patient")
    vol.add_argument("--corrupt-hash", action="store_true",
                     help="Declare a deliberately wrong content_sha256; the job must "
                          "FAIL rather than segment. The patient-safety test.")
    vol.add_argument("--release", action="store_true",
                     help="DELETE the volume at the end, then assert a job against it 404s")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())

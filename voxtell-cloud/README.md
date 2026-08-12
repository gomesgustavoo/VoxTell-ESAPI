# VoxTell Cloud

> Multi-user, queue-backed backend for free-text 3D segmentation from Varian Eclipse.

[**Root README**](../README.md) · [**Wire protocol**](PROTOCOL.md)

The v1 server in this repository was a single-user bridge: sessions in a Python
dict, `--workers 1`, a CT streamed one slice at a time, no auth, and nothing left
after a restart. This is the replacement — a service several Eclipse workstations
can share, that survives a pod dying mid-job, and that keeps one GPU busy without
letting one user monopolise it.

---

## Shape

```
                      voxtell.dicomsegvr.com
                    ┌──────────┴───────────┐
              /v1 → │   api  (×2, no GPU)  │ ← /  console (SPA)
                    └──────────┬───────────┘
                    Postgres   │   jobs queue (FOR UPDATE SKIP LOCKED)
                               ▼
   presigned S3  ◀──   object store   ──▶   worker (×1, GPU)
   (volume in,                              preprocess │ infer │ postprocess
    result out)                                        └─ advisory-lock mutex
                                                          shared with DicomSegVR
```

| | |
|---|---|
| `voxtell_cloud/` | shared, torch-free: LPS↔RAS geometry, contour tracing, wire format |
| `api/` | FastAPI control plane — auth, quota, presigning, job rows. No torch. |
| `worker/` | GPU worker — queue claim, 3-stage pipeline, model lifecycle |
| `console/` | small React SPA: sign in, mint API keys, watch jobs |
| `scripts/` | model fetcher, key minter, end-to-end client |

**The volume never passes through the API.** The client PUTs it straight to object
storage with presigned multipart URLs and the worker reads it from there. That is
what lets the API tier stay a 470 MB image at two replicas no matter how large the
studies get, and it keeps every request under Cloudflare's 100 MB body cap.

---

## Design notes worth knowing before changing things

**Why a queue and not a request/response call.** Inference takes minutes and there
is exactly one GPU. An HTTP request cannot wait that long across Cloudflare, and
two concurrent users would otherwise fight over VRAM. Jobs are rows in Postgres,
claimed with `FOR UPDATE SKIP LOCKED`; a worker that dies mid-job stops
heartbeating and the job is requeued (up to `WORKER_MAX_ATTEMPTS`, then failed).

**Why the GPU mutex.** The node has one RTX 3080 shared with DicomSegVR's
inference pod. The NVIDIA device plugin is set to time-slice it (`replicas: 2`) so
both pods can schedule — but time-slicing shares compute, **not VRAM**. Both
workers therefore take the same Postgres advisory lock, in a separate empty
`gpulock` database (advisory locks are per-database and the two services use
different ones), for the duration of their prediction only. A pod crash drops the
connection and releases the lock; there is no reaper to get wrong.

**Why HU conversion is server-side.** ESAPI's `GetVoxels` returns stored values.
VoxTell z-score-normalises, which is invariant to a linear rescale — but it first
calls `crop_to_nonzero`, which thresholds at 0. Air at −1000 crops differently from
air at 0. The client sends `scaling_slope`/`scaling_intercept` and the server
applies them, so the plugin never touches 100 MB of voxels.

**Why the embedding cache.** VoxTell embeds prompts with a frozen 8 GB Qwen3
backbone. Upstream ships a precomputed bank for its own label set, but our users
type free text, so misses are normal. Computed vectors are persisted to
`prompt_embeddings`, and the backbone is dropped from host RAM after each job — on
a 23 GB box shared with another inference pod, holding it resident is not
affordable, and with the cache in front a reload is rare.

**Upstream is a dependency, not a fork.** `voxtell` is pinned by commit in
`requirements-worker.txt`. In particular `voxtell.server.runner.RemoteInferenceEngine`
does prompt batching sized to free VRAM, halving on OOM, CPU-accumulation fallback
and largest-component cleanup — all reused as-is. Bumping upstream is a one-line
change; do not vendor it back in.

---

## Deploying on the k3s cluster

Manifests live in `~/k3s-platform` (`80`–`85`), namespace `voxtell`.

```bash
# 1. weights (~12 GB, once)
python scripts/fetch_models.py --dest /home/tavulha/voxtell/models

# 2. images. DOCKER_BUILDKIT=0 is required — docker 29 on this host has no buildx.
#    Bump the tag every time: every manifest is imagePullPolicy: IfNotPresent.
TAG=0.1.0
sudo env DOCKER_BUILDKIT=0 docker build -f Dockerfile.api     -t voxtell/api:$TAG .
sudo env DOCKER_BUILDKIT=0 docker build -f Dockerfile.worker  -t voxtell/worker:$TAG .
sudo env DOCKER_BUILDKIT=0 docker build -f Dockerfile.console -t voxtell/console:$TAG console
for i in api worker console; do
  sudo docker save voxtell/$i:$TAG | sudo k3s ctr images import -   # worker takes minutes
done

# 3. secrets, then manifests (API first — it owns the schema)
~/k3s-platform/voxtell-secrets-bootstrap.sh
kubectl apply -f ~/k3s-platform/80-voxtell-namespace.yaml
kubectl apply -f ~/k3s-platform/81-voxtell-config.yaml
kubectl apply -f ~/k3s-platform/82-voxtell-api.yaml
kubectl apply -f ~/k3s-platform/47-nvidia-device-plugin.yaml   # time-slicing
kubectl apply -f ~/k3s-platform/83-voxtell-worker.yaml
kubectl apply -f ~/k3s-platform/84-voxtell-console.yaml
kubectl apply -f ~/k3s-platform/85-voxtell-ingress.yaml
```

The one thing that cannot be scripted from here: adding
`voxtell.dicomsegvr.com` as a public hostname on the Cloudflare tunnel
(→ `http://traefik.kube-system.svc.cluster.local:80`) plus a proxied CNAME. Until
that exists, everything is still reachable in-cluster with a `Host:` header.

### Verifying

```bash
kubectl get node dev -o jsonpath='{.status.capacity.nvidia\.com/gpu}'   # -> 2
kubectl -n voxtell get pods
kubectl -n voxtell logs deploy/voxtell-worker | grep "VoxTell warm"

python scripts/e2e_client.py --base https://voxtell.dicomsegvr.com/v1 \
    --key vxt_... --dicom ~/study --prompt liver --prompt spleen
```

`e2e_client.py` is the Eclipse plugin in Python: it drives the full protocol and
then projects every returned contour point back through the LPS affine to confirm
it lands on the slice it claims, inside the source grid.

---

## Local / on-prem

`docker-compose.yml` runs the same images against Postgres + MinIO on one machine,
with API keys instead of Keycloak — for sites that cannot use the hosted service.
See the comments at the top of that file.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[api,dev]"
.venv/bin/python -m pytest            # geometry + contour tracing, no GPU needed
```

The pure-unit tests cover the parts where a silent sign error yields a plausible but
mirrored result: the LPS affine, the RAS round trip, HU rescaling, and contour
tracing against a synthetic sphere with known geometry. They need no database and
must stay that way — `pytest -m "not pg"` is the guarantee.

### The database-backed tests

The queue is Postgres behaviour, not Python behaviour: `FOR UPDATE SKIP LOCKED`,
advisory locks, `make_interval`, partial-index eligibility, and the fact that
Postgres **rejects `FOR UPDATE` in any query containing a window function** (which
is why the dispatch rank is a stored column and not a `row_number() OVER
(PARTITION BY user_id)`). None of that survives a mock, so those tests want a real
server. They are marked `pg` and **skip cleanly when unconfigured**.

They run against the cluster's own Postgres — `testcontainers` and the
`docker-compose.yml` service both need the Docker socket, which is root-only on the
dev box. Once, as `postgres`, because the `voxtell` role has no `CREATEDB`:

```bash
export KUBECONFIG=/home/tavulha/.kube/config
kubectl -n platform exec postgres-0 -- psql -U postgres -c 'CREATE DATABASE voxtell_test OWNER voxtell'
```

Then per shell:

```bash
export PYTHONPATH=$PWD
export VOXTELL_TEST_DATABASE_URL="postgresql://voxtell:$(kubectl -n voxtell get secret voxtell-secrets -o jsonpath='{.data.DB_PASSWORD}' | base64 -d)@$(kubectl -n platform get pod postgres-0 -o jsonpath='{.status.podIP}'):5432/voxtell_test"
.venv/bin/python -m pytest -m pg
```

Each session creates and drops its own `test_<hex>` **schema** inside that database,
so parallel runs never collide and no test can reach a production row even if the
DSN is pointed somewhere unexpected.

Two conventions in `tests/conftest.py` that are load-bearing:

- **Time is written in SQL, never patched in Python.** Every reclaim, aging and
  staleness predicate is evaluated by Postgres via `now()`, so tests place a job in
  the past with `queued_at="now() - interval '11 minutes'"`. `freezegun` cannot move
  the database's clock and would prove nothing.
- **`worker.db.get_engine` is the single patch seam.** `worker/job.py` and
  `worker/embeddings.py` call `db.get_engine()` through the module rather than
  importing the name, so one `monkeypatch.setattr` repoints all of them. A
  `from .db import get_engine` anywhere would silently escape that.

Tests marked `xfail(strict=True)` describe behaviour a later phase introduces —
strict means they **fail the suite once they start passing**, so they cannot rot
into decoration.

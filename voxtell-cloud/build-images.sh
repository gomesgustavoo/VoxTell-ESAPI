#!/usr/bin/env bash
# build-images.sh — build one VoxTell image and import it into k3s containerd.
#
# k3s uses containerd (NOT the docker daemon) for pods, so a `docker build` image is
# invisible to the kubelet until it is imported into containerd's k8s.io namespace:
#   docker build  ->  docker save  ->  sudo k3s ctr images import -
# It must be `k3s ctr` (k8s.io namespace); plain `ctr` lands in the wrong namespace and
# the kubelet never sees it. Sibling of k3s-platform/build-images.sh, which has no
# voxtell entry because these four images have four different contexts and one of them
# (worker) is an 11 GB import that you never want to run by accident alongside others.
#
# Usage: ./build-images.sh <landing|console|api|worker> <TAG>
#        ./build-images.sh landing 0.2.0
#
# BUMP THE TAG ON EVERY REBUILD. Manifests use imagePullPolicy: IfNotPresent and these
# images are local-only, so reusing a tag means pods keep serving the cached layer.
#
# WHY THE TOKEN CHECK IS HERE AND NOT IN THE DOCKERFILES
# design/sync-tokens.py generates landing/assets/tokens.v2.css and
# console/src/tokens.generated.css from design/tokens.css. It cannot run inside either
# image build, because the landing's build context is landing/ and the console's is
# console/, and neither can reach a path above itself — which is the entire reason the
# tokens are generated rather than shared from one file. So the staleness guard lives
# here, in the one place that can see all three paths at once. Skipping it ships a
# surface whose palette silently disagrees with the source of truth.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"
TAG="${2:-}"

if [[ -z "$TARGET" || -z "$TAG" ]]; then
  echo "usage: $0 <landing|console|api|worker> <TAG>" >&2
  exit 2
fi

echo "==> checking design tokens are in sync"
python3 "${HERE}/design/sync-tokens.py" --check

case "$TARGET" in
  landing)  DOCKERFILE="${HERE}/landing/Dockerfile"; CONTEXT="${HERE}/landing" ;;
  console)  DOCKERFILE="${HERE}/Dockerfile.console"; CONTEXT="${HERE}/console" ;;
  api)      DOCKERFILE="${HERE}/Dockerfile.api";     CONTEXT="${HERE}" ;;
  worker)   DOCKERFILE="${HERE}/Dockerfile.worker";  CONTEXT="${HERE}" ;;
  *) echo "unknown target: $TARGET" >&2; exit 2 ;;
esac

REF="voxtell/${TARGET}:${TAG}"

if sudo k3s ctr images ls -q | grep -qx "docker.io/${REF}"; then
  echo "ERROR: ${REF} is already in containerd. Bump the tag — IfNotPresent means" >&2
  echo "       the kubelet would keep serving the existing image." >&2
  exit 1
fi

# Disk headroom before the import. The kubelet's imageGCHighThresholdPercent defaults to
# 85, and it has deleted a just-imported image seconds after the import finished.
USED="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
echo "==> root filesystem at ${USED}%"
if [[ "$USED" -ge 80 ]]; then
  echo "ERROR: root fs at ${USED}%. Free space first — image GC starts at 85% and" >&2
  echo "       will evict the image you are about to import." >&2
  exit 1
fi

echo "==> building ${REF} (context ${CONTEXT})"
sudo env DOCKER_BUILDKIT=0 docker build -f "$DOCKERFILE" -t "$REF" "$CONTEXT"

echo "==> importing ${REF} into k3s containerd (k8s.io namespace)"
sudo docker save "$REF" | sudo k3s ctr images import -

echo "==> verifying"
sudo k3s ctr images ls -q | grep -x "docker.io/${REF}" \
  || { echo "ERROR: ${REF} not present in containerd after import" >&2; exit 1; }

echo "==> done: ${REF}"

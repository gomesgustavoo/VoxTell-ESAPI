#!/usr/bin/env bash
# =============================================================================
# run.sh — Start the VoxTell ESAPI Bridge API server
#
# Usage:
#   bash run.sh
#   VOXTELL_MODEL_DIR=/path/to/model bash run.sh
#
# All options can also be set in a .env file at the repo root.
# See .env.example for available variables.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Load .env if present
# ---------------------------------------------------------------------------
if [[ -f ".env" ]]; then
    echo "[run.sh] Loading .env"
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# ---------------------------------------------------------------------------
# 2. Defaults for optional variables
# ---------------------------------------------------------------------------
VOXTELL_HOST="${VOXTELL_HOST:-0.0.0.0}"
VOXTELL_PORT="${VOXTELL_PORT:-8000}"
VOXTELL_DEVICE="${VOXTELL_DEVICE:-cuda}"
VOXTELL_GPU_ID="${VOXTELL_GPU_ID:-0}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-voxtell}"

# ---------------------------------------------------------------------------
# 3. Validate required VOXTELL_MODEL_DIR
# ---------------------------------------------------------------------------
if [[ -z "${VOXTELL_MODEL_DIR:-}" ]]; then
    echo "[run.sh] ERROR: VOXTELL_MODEL_DIR is not set." >&2
    echo "         Export it or add it to a .env file at the repo root." >&2
    echo "         Example: VOXTELL_MODEL_DIR=/path/to/model bash run.sh" >&2
    exit 1
fi

if [[ ! -d "$VOXTELL_MODEL_DIR" ]]; then
    echo "[run.sh] ERROR: VOXTELL_MODEL_DIR does not exist: $VOXTELL_MODEL_DIR" >&2
    exit 1
fi

if [[ ! -f "$VOXTELL_MODEL_DIR/plans.json" ]]; then
    echo "[run.sh] ERROR: plans.json not found in VOXTELL_MODEL_DIR=$VOXTELL_MODEL_DIR" >&2
    exit 1
fi

if [[ ! -f "$VOXTELL_MODEL_DIR/fold_0/checkpoint_final.pth" ]]; then
    echo "[run.sh] ERROR: fold_0/checkpoint_final.pth not found in VOXTELL_MODEL_DIR=$VOXTELL_MODEL_DIR" >&2
    exit 1
fi

export VOXTELL_MODEL_DIR VOXTELL_DEVICE VOXTELL_GPU_ID

# ---------------------------------------------------------------------------
# 4. Activate conda environment
# ---------------------------------------------------------------------------
CONDA_BASE=""
for candidate in \
    "$HOME/miniconda3" \
    "$HOME/anaconda3" \
    "/opt/miniconda3" \
    "/opt/anaconda3" \
    "/usr/local/miniconda3" \
    "/usr/local/anaconda3"; do
    if [[ -f "${candidate}/etc/profile.d/conda.sh" ]]; then
        CONDA_BASE="$candidate"
        break
    fi
done

if [[ -z "$CONDA_BASE" ]]; then
    echo "[run.sh] ERROR: conda not found." >&2
    echo "         Install Miniconda/Anaconda or set CONDA_BASE manually." >&2
    exit 1
fi

# shellcheck disable=SC1091
source "${CONDA_BASE}/etc/profile.d/conda.sh"

conda activate "$CONDA_ENV_NAME" 2>/dev/null || {
    echo "[run.sh] ERROR: Failed to activate conda environment '${CONDA_ENV_NAME}'." >&2
    echo "         Create it with: conda create -n ${CONDA_ENV_NAME} python=3.12" >&2
    exit 1
}

echo "[run.sh] Conda environment '${CONDA_ENV_NAME}' active ($(python --version))"

# ---------------------------------------------------------------------------
# 5. Ensure voxtell package is installed (editable)
# ---------------------------------------------------------------------------
if ! python -c "import voxtell" 2>/dev/null; then
    echo "[run.sh] Installing voxtell in editable mode..."
    pip install -e "." --quiet
fi

# ---------------------------------------------------------------------------
# 6. Ensure API extras are installed (pydantic-settings etc.)
# ---------------------------------------------------------------------------
if ! python -c "import pydantic_settings" 2>/dev/null; then
    echo "[run.sh] Installing API extras (first-time setup)..."
    pip install -e ".[api]" --quiet
fi

# ---------------------------------------------------------------------------
# 7. Startup summary
# ---------------------------------------------------------------------------
echo ""
echo "  VoxTell ESAPI Bridge"
echo "  ────────────────────────────────────────"
echo "  Model dir : ${VOXTELL_MODEL_DIR}"
echo "  Device    : ${VOXTELL_DEVICE}:${VOXTELL_GPU_ID}"
echo "  Listen    : http://${VOXTELL_HOST}:${VOXTELL_PORT}"
echo "  Health    : http://${VOXTELL_HOST}:${VOXTELL_PORT}/health"
echo "  Docs      : http://${VOXTELL_HOST}:${VOXTELL_PORT}/docs"
echo "  ────────────────────────────────────────"
echo ""

# ---------------------------------------------------------------------------
# 8. Start the server (exec replaces this shell process)
# ---------------------------------------------------------------------------
exec uvicorn api.main:app \
    --host "${VOXTELL_HOST}" \
    --port "${VOXTELL_PORT}" \
    --workers 1

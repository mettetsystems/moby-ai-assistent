#!/usr/bin/env bash
# MobyAI bootstrap: venv + package install, then the interactive setup wizard.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

NON_INTERACTIVE=0
SKIP_INSTALL=0
DEV_EXTRAS=1
SETUP_ARGS=()

usage() {
  cat <<'EOF'
MobyAI bootstrap + setup wizard

This script prepares Python, then runs `mobyai setup`, which:
  - scans NVIDIA GPUs and proposes a layout (largest GPU -> planner/worker)
  - installs CUDA llama.cpp from llama.app (not the Fedora ROCm package)
  - writes ~/MobyAI/env.sh so ~/.local/bin/llama wins on PATH
  - downloads GGUF weights and healthchecks each role

Usage:
  ./scripts/setup.sh
  ./scripts/setup.sh --yes
  ./scripts/setup.sh --yes --output ./config.yaml --force --skip-hf-token

Options (this script):
  -h, --help             Show this help
  -y, --yes              Pass --non-interactive to the wizard
  --non-interactive      Same as --yes
  --skip-install         Skip venv creation and pip (use the current Python)
  --no-dev               pip install -e .  without pytest extras
  --                       Remaining flags go to `mobyai setup`

Common wizard flags:
  --output PATH
  --force
  --preset default|external|custom
  --hf-token TOKEN
  --skip-hf-token
  --download-models
  --skip-download
  --skip-runtime
  --skip-endpoint-test
  --planner-device INDEX
  --orchestrator-device INDEX
  --worker-device INDEX

After setup:
  source ~/MobyAI/env.sh
  mobyai --config ./config.yaml start

Full reference: docs/setup.md
EOF
}

log() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "Missing required command: $1"
    exit 1
  fi
}

python_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null
}

find_python() {
  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]] && python_ok "${VIRTUAL_ENV}/bin/python"; then
    printf '%s\n' "${VIRTUAL_ENV}/bin/python"
    return
  fi
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      command -v "$candidate"
      return
    fi
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -y|--yes|--non-interactive)
      NON_INTERACTIVE=1
      SETUP_ARGS+=(--non-interactive)
      shift
      ;;
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --no-dev)
      DEV_EXTRAS=0
      shift
      ;;
    --)
      shift
      SETUP_ARGS+=("$@")
      break
      ;;
    --output|--mode|--preset|--workspace|--database|--hf-token|--hf-token-file|--planner-model|--planner-endpoint|--planner-device|--orchestrator-model|--orchestrator-endpoint|--orchestrator-device|--worker-model|--worker-endpoint|--worker-device)
      SETUP_ARGS+=("$1")
      if [[ $# -lt 2 ]]; then
        log "Option $1 requires a value"
        exit 2
      fi
      SETUP_ARGS+=("$2")
      shift 2
      ;;
    --force|--no-directories|--skip-hf-token|--download-models|--skip-download|--skip-runtime|--skip-endpoint-test)
      SETUP_ARGS+=("$1")
      shift
      ;;
    *)
      log "Unknown option: $1"
      log "Try: ./scripts/setup.sh --help"
      exit 2
      ;;
  esac
done

log "MobyAI bootstrap"
log "Repository: $REPO_ROOT"
log "The setup wizard handles GPU layout, CUDA llama.cpp, weights, and endpoint tests."
log "Written guide: docs/setup.md"

step "1/3  Repository + Python"
if [[ ! -f "$REPO_ROOT/pyproject.toml" || ! -d "$REPO_ROOT/src/mobyai" ]]; then
  log "This script must run from a MobyAI checkout."
  exit 1
fi
if ! PYTHON=$(find_python); then
  log "Python 3.12 or newer was not found on PATH."
  exit 1
fi
log "Python: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"

VENV_PY="$PYTHON"
if [[ "$SKIP_INSTALL" -eq 1 ]]; then
  step "2/3  Skip venv (--skip-install)"
else
  step "2/3  Virtualenv + package"
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    VENV_PY="${VIRTUAL_ENV}/bin/python"
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    VENV_PY="$REPO_ROOT/.venv/bin/python"
  else
    "$PYTHON" -m venv "$REPO_ROOT/.venv"
    VENV_PY="$REPO_ROOT/.venv/bin/python"
  fi
  if ! python_ok "$VENV_PY"; then
    log ".venv is not Python 3.12+. Remove it and re-run."
    exit 1
  fi
  "$VENV_PY" -m pip install -U pip >/dev/null
  if [[ "$DEV_EXTRAS" -eq 1 ]]; then
    (cd "$REPO_ROOT" && "$VENV_PY" -m pip install -e ".[dev]")
  else
    (cd "$REPO_ROOT" && "$VENV_PY" -m pip install -e .)
  fi
  log "Installed mobyai into $REPO_ROOT/.venv"
fi

step "3/3  Setup wizard (mobyai setup)"
if command -v nvidia-smi >/dev/null 2>&1; then
  log "NVIDIA GPUs:"
  nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null || true
else
  log "nvidia-smi not found — wizard will use reference GPU indices."
fi
log "Launching wizard…"
"$VENV_PY" -m mobyai setup "${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}"

cat <<EOF

Bootstrap complete.

In new shells:
  source $REPO_ROOT/.venv/bin/activate
  source ~/MobyAI/env.sh

Then:
  mobyai --config ./config.yaml doctor
  mobyai --config ./config.yaml start

EOF

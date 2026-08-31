#!/usr/bin/env bash
# Walk through installing MobyAI, llama.cpp, GGUF weights, and an endpoint test.
# `mobyai start` then reuses a live server or spawns llama serve.
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
MobyAI first-run setup

Walks through, in order:
  1. Confirm you are in the repository
  2. Find Python 3.12 or newer
  3. Create .venv (unless SKIP or already active)
  4. pip install -e ".[dev]"
  5. Run `mobyai setup` (HF token, llama.cpp, GGUF download, endpoint test)
  6. Print remaining operator steps (doctor, start, first project)

Usage:
  ./scripts/setup.sh
  ./scripts/setup.sh --yes
  ./scripts/setup.sh --yes --output ./config.yaml --force --skip-hf-token --skip-download

Options (this script):
  -h, --help             Show this help
  -y, --yes              Do not pause between steps; pass --non-interactive to the wizard
  --non-interactive      Same as --yes
  --skip-install         Skip venv creation and pip (use the current Python)
  --no-dev               pip install -e .  without pytest extras
  --                       Remaining flags go to `mobyai setup`

Options forwarded to `mobyai setup`:
  --output PATH
  --force
  --preset default|external|custom
  --mode external|managed
  --hf-token TOKEN
  --hf-token-file PATH
  --skip-hf-token
  --download-models
  --skip-download
  --skip-runtime
  --skip-endpoint-test
  --workspace PATH
  --database PATH
  --planner-model ID
  --planner-endpoint URL
  --planner-device INDEX|cpu
  --orchestrator-model ID
  --orchestrator-endpoint URL
  --orchestrator-device INDEX|cpu
  --worker-model ID
  --worker-endpoint URL
  --worker-device INDEX|cpu
  --no-directories

Read docs/setup.md for the same steps written out by hand.
EOF
}

log() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

pause() {
  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    read -r -p "Press Enter to continue..." _
  fi
}

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

log "MobyAI setup walkthrough"
log "Repository: $REPO_ROOT"
log "This writes config, installs llama.cpp, stores an optional Hugging Face token,"
log "downloads GGUF weights, and tests each role's /v1/models endpoint."
log "Full written form of these steps: docs/setup.md"
pause

step "1/6  Confirm repository layout"
if [[ ! -f "$REPO_ROOT/pyproject.toml" || ! -d "$REPO_ROOT/src/mobyai" ]]; then
  log "This script must run from a MobyAI checkout (missing pyproject.toml or src/mobyai)."
  exit 1
fi
log "Found pyproject.toml and src/mobyai."
pause

step "2/6  Find Python 3.12+"
need_cmd uname
if ! PYTHON=$(find_python); then
  log "Python 3.12 or newer was not found on PATH."
  log "Install Python 3.12+, then re-run this script."
  exit 1
fi
log "Using: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
pause

VENV_PY="$PYTHON"
if [[ "$SKIP_INSTALL" -eq 1 ]]; then
  step "3/6  Skip venv (--skip-install)"
  log "Using current interpreter: $VENV_PY"
else
  step "3/6  Create virtualenv at $REPO_ROOT/.venv"
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    log "Already inside a virtualenv: $VIRTUAL_ENV"
    VENV_PY="${VIRTUAL_ENV}/bin/python"
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    log "Reusing existing $REPO_ROOT/.venv"
    VENV_PY="$REPO_ROOT/.venv/bin/python"
    if ! python_ok "$VENV_PY"; then
      log ".venv exists but is not Python 3.12+. Remove it and re-run, or pass --skip-install."
      exit 1
    fi
  else
    log "Creating $REPO_ROOT/.venv with $PYTHON"
    "$PYTHON" -m venv "$REPO_ROOT/.venv"
    VENV_PY="$REPO_ROOT/.venv/bin/python"
  fi
  log "Virtualenv Python: $VENV_PY"
  pause

  step "4/6  Install the mobyai package"
  "$VENV_PY" -m pip install -U pip
  if [[ "$DEV_EXTRAS" -eq 1 ]]; then
    log "pip install -e \".[dev]\"  (harness + pytest)"
    (
      cd "$REPO_ROOT"
      "$VENV_PY" -m pip install -e ".[dev]"
    )
  else
    log "pip install -e .  (harness only)"
    (
      cd "$REPO_ROOT"
      "$VENV_PY" -m pip install -e .
    )
  fi
  log "Installed. Console script: $("$VENV_PY" -c 'import shutil; print(shutil.which("mobyai") or "python -m mobyai")')"
  pause
fi

if [[ "$SKIP_INSTALL" -eq 1 ]]; then
  step "4/6  Skip pip (--skip-install)"
fi

step "5/6  Write YAML, install llama.cpp, pull models, test endpoints (mobyai setup)"
log "Defaults: Hugging Face token -> ~/MobyAI/secrets/hf_token"
log "          managed llama.cpp, GPU 0 planner+worker, GPU 1 orchestrator"
log "          Qwen/Qwen3-32B-GGUF:Q4_K_M, Qwen/Qwen2.5-7B-Instruct-GGUF:Q5_K_M,"
log "          unsloth/Qwen3.5-35B-A3B-GGUF:Q4_K_M"
log "After the token prompt, Enter installs llama.cpp, downloads GGUF files, and healthchecks."
log "Press Enter to keep a default, or type a custom model / endpoint / device."
if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
  log "Non-interactive: those defaults plus any flags you passed."
  log "Pass --download-models to install llama.cpp, pull weights, and test endpoints."
fi
pause
"$VENV_PY" -m mobyai setup "${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}"

step "6/6  What is still on you"
cat <<EOF
Activate the venv in new shells:
  source $REPO_ROOT/.venv/bin/activate

Then:
  1. Review the config (reference models are already filled in; override any field).
  2. If you skipped the Hugging Face token, export HF_TOKEN or write ~/MobyAI/secrets/hf_token
  3. mobyai --config PATH config validate
  4. If services.mode is external, start three OpenAI-compatible servers on the configured ports.
     If managed, setup already installed llama.cpp unless you skipped provision.
  5. mobyai --config PATH doctor
  6. mobyai --config PATH start
     (reuses a live endpoint; otherwise spawns llama serve)
  7. mobyai --config PATH init --name demo --objective "Create a small REST API with an SQLite-backed task manager."
  8. mobyai --config PATH run demo

Tests (no GPU required):
  pytest tests/unit tests/integration

EOF
log "Done. Details: docs/setup.md"

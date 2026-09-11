#!/usr/bin/env bash
# Launch the installed application; defaults to the operator console.
set -euo pipefail
REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
usage() {
  cat <<'HELP'
Usage: ./scripts/start.sh [--config PATH] [COMMAND [ARGS...]]

No command opens the operator console (ui).
Examples:
  ./scripts/start.sh
  ./scripts/start.sh ui --no-browser
  ./scripts/start.sh run demo
  ./scripts/start.sh status demo
  ./scripts/start.sh --config /path/to/config.yaml run demo

Uses the repository .venv when present, otherwise mobyai on PATH.
Does not install packages. Ctrl+C stops the foreground command.
HELP
}
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  usage
  exit 0
fi
CONFIG=${MOBYAI_CONFIG:-$REPO_ROOT/config.yaml}
if [[ ${1:-} == --config ]]; then
  [[ $# -ge 2 && -n $2 ]] || { echo '--config requires a path' >&2; exit 2; }
  CONFIG=$2
  shift 2
fi
# Resolve a caller-relative path before changing directories.
[[ $CONFIG == /* ]] || CONFIG="$PWD/$CONFIG"
[[ -f $CONFIG ]] || { echo "Config not found: $CONFIG. Run ./scripts/setup.sh first or supply --config." >&2; exit 1; }
if [[ -x "$REPO_ROOT/.venv/bin/mobyai" ]]; then
  source "$REPO_ROOT/.venv/bin/activate"
  MOBYAI_BIN="$REPO_ROOT/.venv/bin/mobyai"
else
  MOBYAI_BIN=$(command -v mobyai) || { echo 'MobyAI is not installed. Run ./scripts/setup.sh first.' >&2; exit 1; }
fi
if [[ -f "$HOME/MobyAI/env.sh" ]]; then
  source "$HOME/MobyAI/env.sh"
fi
cd -- "$REPO_ROOT"
[[ $# -gt 0 ]] || set -- ui
printf 'Launching MobyAI %s using %s\n' "$1" "$CONFIG"
exec "$MOBYAI_BIN" --config "$CONFIG" "$@"

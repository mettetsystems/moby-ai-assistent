#!/usr/bin/env bash
# Restart only this checkout's idle MobyAI console, then run it in the foreground.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo 'MobyAI virtual environment is missing. Run scripts/setup.sh first.' >&2
  exit 1
fi
exec "$python_bin" "$repo_dir/scripts/restart.py" "$@"

#!/usr/bin/env bash
# Standalone stdio MCP entry point; MobyAI starts this server on demand internally.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ${1:-} == --help ]]; then
  echo 'Local research MCP server (stdio). Install: .venv/bin/python -m pip install -e ".[research]"'
  echo 'Optional environment: BRAVE_SEARCH_API_KEY, SEMANTIC_SCHOLAR_API_KEY, MOBYAI_RESEARCH_CACHE'
  exit 0
fi
cd -- "$repo_dir"
exec "$repo_dir/.venv/bin/python" -m mobyai.research.server

#!/usr/bin/env bash
# View local MobyAI traces using the same configuration as the console.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
exec "$repo_dir/.venv/bin/python" - "$repo_dir/config.yaml" <<'PY'
import os
import sys
from mobyai.models.config import AppConfig
config = AppConfig.from_yaml(os.environ.get('MOBYAI_CONFIG', sys.argv[1]))
uri = os.environ.get('MLFLOW_TRACKING_URI') or config.tracing.tracking_uri
if not uri:
    uri = 'sqlite:///' + str(config.persistence.database_path.parent.resolve() / 'mlflow.db')
if not uri.startswith('sqlite:///'):
    raise SystemExit('This helper serves local SQLite traces only. Open your configured MLflow server instead.')
os.execv(sys.executable, [sys.executable, '-m', 'mlflow', 'ui', '--backend-store-uri', uri,
                        '--host', '127.0.0.1', '--port', '5000'])
PY

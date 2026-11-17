---
name: mobyai-setup
description: >-
  Set up, configure, and run the MobyAI harness (venv, Hugging Face token,
  llama.cpp, YAML config, doctor, start, first project, operator console).
  Use when installing MobyAI, running scripts/setup.sh or mobyai setup,
  debugging doctor/start, or walking an operator through a first live run.
---

# MobyAI operator setup

MobyAI is not a chat app. Models propose JSON; Python owns state, tests, files, and the DAG. Setup produces a venv, YAML, optional HF token file, llama.cpp, and workspace dirs.

Full field-by-field walkthrough: [docs/setup.md](docs/setup.md).

## Quick path

From the repository root, Python 3.12+:

```bash
./scripts/setup.sh
source .venv/bin/activate
source ~/MobyAI/env.sh   # if present; puts llama on PATH
```

Interactive setup: Enter keeps defaults. After the token prompt, Enter installs llama.cpp from llama.app, downloads reference GGUF specs, healthchecks `/v1/models`, then stops those test processes.

Non-interactive (no Hub download):

```bash
./scripts/setup.sh --yes --output ./config.yaml --force --skip-hf-token --skip-download
```

Do not pass `--hf-token` on the command line (`ps` can see it). Prefer the prompt, `HF_TOKEN`, or `--hf-token-file`.

## Manual equivalent

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
mobyai setup
```

`pytest` and CLI work without GPUs. Live `mobyai run` needs three OpenAI-compatible endpoints or managed `llama serve`.

## Config discovery

First match wins:

1. `mobyai --config PATH`
2. `MOBYAI_CONFIG`
3. `./config.yaml`
4. `./config/default.yaml`
5. `~/MobyAI/config.yaml`

`mobyai setup` writes `./config.yaml` by default. Unknown YAML keys are rejected (`AppConfig`).

Templates: `config/default.yaml` (external), `config/examples/managed.yaml` (spawn llama.cpp), `config/examples/dual_gpu.yaml`.

## Secrets

- Token string never goes in YAML. Path only: `huggingface.token_file` (default `~/MobyAI/secrets/hf_token`, mode 0600).
- `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` override the file for the process.
- Managed `start`/`run` copy the resolved token into spawned llama.cpp env.

## Reference models (overridable)

| Role | GGUF spec | Endpoint | GPU |
|------|-----------|----------|-----|
| Planner | `Qwen/Qwen3-32B-GGUF:Q4_K_M` | `http://127.0.0.1:9802` | 0 |
| Orchestrator | `Qwen/Qwen2.5-7B-Instruct-GGUF:Q5_K_M` | `http://127.0.0.1:9801` | 1 |
| Worker | `unsloth/Qwen3.5-35B-A3B-GGUF:Q4_K_M` | `http://127.0.0.1:9800` | 0 |

Preset `default` = **managed** llama.cpp, dual GPU. Planner+Worker share GPU 0 (swap; not coresident). Orchestrator on GPU 1. `external` = same ids, you start servers. Context: Planner 32768, Orchestrator 16384, Worker 65536 (`{ctx}` in `launch`); Worker packer budget is 48000.

Install CUDA llama.cpp from llama.app, not a distro ROCm `llama` package. Check `llama` / `llama-server` on PATH, `~/.llama-app/llama`, `~/.local/bin/llama`.

## Modes

| `services.mode` | Start servers | Stop |
|-----------------|---------------|------|
| `external` | Operator | Operator (`doctor` only healthchecks `/v1/models`) |
| `managed` | `mobyai start` / `run` via `launch` argv | `mobyai stop` |

`managed` sets `CUDA_VISIBLE_DEVICES` from `hardware.<role>.device`. Missing `nvidia-smi` is a doctor **warning** in external mode and a **failure** in managed.

`--non-interactive` skips provision unless `--download-models`. `--skip-download` skips install, Hub pull, and endpoint test.

## After install: check then run

```bash
mobyai --config ./config.yaml config validate
mobyai --config ./config.yaml models
mobyai --config ./config.yaml doctor
mobyai --config ./config.yaml start
```

`start` reuses a live `/v1/models` endpoint; otherwise it spawns `llama serve`.

Console (default port 8743):

```bash
./scripts/start.sh
# or
mobyai --config ./config.yaml ui
```

`./scripts/start.sh` activates `.venv`, loads `~/MobyAI/env.sh`, opens UI. Restart/shutdown: `scripts/restart.sh`, `scripts/shutdown.sh`. Settings writes `{config}.overlay.yaml`; a running `run` reloads overlay on the next generate.

First project:

```bash
mobyai --config ./config.yaml init --name demo --objective "Create a small REST API with an SQLite-backed task manager."
mobyai --config ./config.yaml run demo
mobyai --config ./config.yaml status demo
```

`resume` reloads SQLite, not chat history. `init` without `--objective` only ensures config, DB parent, and workspace root.

Tests (no GPU):

```bash
pytest tests/unit tests/integration
```

## Failures

| Symptom | Check |
|---------|--------|
| No configuration file | `--config`, `mobyai setup`, or `MOBYAI_CONFIG` |
| Invalid configuration | Unknown key / missing `models.planner` → `config validate` |
| doctor FAIL on endpoint | Process down, wrong port, http vs TLS |
| doctor warn hf token | Token file, `HF_TOKEN`, or setup prompt |
| Hub 401 | Token lacks repo access |
| doctor FAIL nvidia-smi in managed | GPU driver / run external or `cpu` devices |

Do not raise `runtime.max_task_attempts` above 2 to “keep trying forever” (retry once, then escalate).

## Extra extras

Optional pip extras after the core install: `.[vault]`, `.[sandbox]`, `.[tracing]`, `.[research]`, `.[archaeology]`. Vault tokens are not written to YAML or SQLite. See README and `docs/sandbox.md` only when the user asked for those features.

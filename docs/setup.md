# Set up MobyAI

This is the operator walkthrough. Do these steps in order. The same sequence is automated by [`scripts/setup.sh`](../scripts/setup.sh).

MobyAI is not a chat app. After setup you will have:

1. A Python package (`mobyai`) on your PATH.
2. A YAML file with **default models and a default runtime deployment** (managed llama.cpp, dual GPU). You can override any field.
3. A Hugging Face token file at `~/MobyAI/secrets/hf_token` (mode 0600) if you pasted a token. The token is **not** stored in YAML.
4. The llama.cpp `llama` binary (via https://llama.app) and GGUF weights in the Hub cache, plus a passing `/v1/models` check (unless you skip provision).
5. Empty directories for project workspaces.

Press Enter at each prompt to keep the default, or type a custom value. After the token prompt, Enter installs llama.cpp, downloads the three GGUF models, and tests each endpoint. Setup then stops those test processes. `mobyai start` reuses a live server or spawns `llama serve`.

---

## 0. What you need before starting

Required:

- A checkout of this repository
- **Python 3.12 or newer** (`python3 --version`)
- `pip` (comes with the venv the script creates)
- Disk write access for the workspace root, SQLite path, `~/MobyAI/secrets/`, and the Hugging Face cache (reference models are tens of GB)
- Optional: a Hugging Face token (`HF_TOKEN` or pasted during setup) for gated Hub repos

Required later, before `mobyai run` against real models:

- Three OpenAI-compatible HTTP servers (Planner, Orchestrator, Worker), **or** llama.cpp from setup (`llama` on PATH / `~/.llama-app/llama`)
- Model ids that those servers actually serve (`GET /v1/models`). Setup fills reference Qwen ids; override them if you serve something else.

Not required for install or for `pytest`:

- NVIDIA GPUs
- vLLM
- A filled-in model id (tests inject fakes)

Reference hardware in the original spec is dual GPU (Planner+Worker on one device, Orchestrator isolated). That layout is **configuration**, not code. One GPU, CPU, or other device maps are valid if you write them in YAML.

---

## 1. Recommended: run the walkthrough script

From the repository root:

```bash
chmod +x scripts/setup.sh   # once, if the bit is not set
./scripts/setup.sh
```

The script pauses after each step so you can read what it is about to do. It will:

| Step | What happens | What you do |
|------|----------------|-------------|
| 1 | Confirms `pyproject.toml` and `src/mobyai/` exist | Press Enter |
| 2 | Finds Python ≥ 3.12 on PATH | Install Python if it exits |
| 3 | Creates or reuses `./.venv` | Nothing unless `.venv` is the wrong version |
| 4 | `pip install -e ".[dev]"` into that venv | Wait |
| 5 | Runs `python -m mobyai setup` | Hugging Face token, then Enter to install llama.cpp, download GGUF weights, and test endpoints |
| 6 | Prints doctor / first-project commands | `doctor` / `start` / first project |

Reference defaults (still overridable):

| Role | Hugging Face GGUF spec | Endpoint | GPU |
|------|------------------------|----------|-----|
| Planner | `Qwen/Qwen3-32B-GGUF:Q4_K_M` | `http://127.0.0.1:9802` | 0 |
| Orchestrator | `Qwen/Qwen2.5-7B-Instruct-GGUF:Q5_K_M` | `http://127.0.0.1:9801` | 1 |
| Worker | `unsloth/Qwen3.5-35B-A3B-GGUF:Q4_K_M` | `http://127.0.0.1:9800` | 0 |

Runtime preset `default` is **managed** llama.cpp (MobyAI spawns `llama serve`). `external` uses the same model ids but you start the servers. `custom` asks for mode first.

Non-interactive (CI or “just write a default config.yaml here”):

```bash
./scripts/setup.sh --yes --output ./config.yaml --force --skip-hf-token --skip-download
```

With a token (prefer the prompt; argv is visible in `ps`):

```bash
./scripts/setup.sh --yes --output ./config.yaml --force --hf-token-file ~/MobyAI/secrets/hf_token
# then paste the token when prompted, or:
export HF_TOKEN=hf_...
./scripts/setup.sh --yes --force --download-models
```

Useful flags:

```text
./scripts/setup.sh --help
./scripts/setup.sh --yes --mode managed --output ~/MobyAI/config.yaml
./scripts/setup.sh --skip-install --yes --output ./config.yaml --force
```

`--skip-install` uses whatever `python` you already have (must still be 3.12+ and have `mobyai` installed).

After the script:

```bash
source .venv/bin/activate
```

Every later `mobyai` / `pytest` command in this document assumes that venv is active.

---

## 2. Manual setup (same steps, no script)

Use this if you cannot run bash, or you want each command in your notes.

### 2.1 Create a virtualenv

```bash
cd /path/to/moby-ai-assistent
python3 --version          # must print 3.12 or newer
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Confirm the console script exists:

```bash
mobyai --version
# or
python -m mobyai --version
```

### 2.2 Write a config file

Either copy a template and edit it, or use the wizard.

**Wizard (Enter keeps defaults; type to override):**

```bash
mobyai setup
# Hugging Face token -> ~/MobyAI/secrets/hf_token
# then downloads Qwen/Qwen3-32B, Qwen/Qwen2.5-7B-Instruct, Qwen/Qwen3.5-35B-A3B
# preset default = managed dual-GPU + reference Qwen models
```

**Wizard, no prompts:**

```bash
mobyai setup --non-interactive --output ./config.yaml --force \
  --preset default \
  --hf-token-file ~/MobyAI/secrets/hf_token \
  --skip-hf-token \
  --skip-download \
  --workspace ~/MobyAI/projects \
  --database ~/MobyAI/mobyai.db \
  --planner-model Qwen/Qwen3-32B \
  --orchestrator-model Qwen/Qwen2.5-7B-Instruct \
  --worker-model Qwen/Qwen3.5-35B-A3B
```

Replace any `--*-model` / `--*-endpoint` / `--*-device` to customize without answering prompts. `--preset external` keeps those models but does not spawn vLLM. `--preset custom --mode managed` asks you to confirm mode in interactive runs.

**Copy a template instead of the wizard:**

```bash
mkdir -p ~/MobyAI
cp config/default.yaml ~/MobyAI/config.yaml          # external servers (default)
# or
cp config/examples/managed.yaml ~/MobyAI/config.yaml # MobyAI spawns vLLM
# or
cp config/examples/dual_gpu.yaml ~/MobyAI/config.yaml
```

Then open the file only if you need to change a default. Hugging Face ids are already filled in.

### Hugging Face token

The token is **never** stored in YAML.

| Place | Role |
|-------|------|
| Prompt during `mobyai setup` | Writes `~/MobyAI/secrets/hf_token` with mode `0600` |
| `huggingface.token_file` in YAML | Path only |
| `HF_TOKEN` or `HUGGING_FACE_HUB_TOKEN` | Overrides the file for the current process |
| `.env` / `.env.example` | Optional local export; gitignored except the example |

Managed `start` / `run` copies the resolved token into the spawned llama.cpp environment. Interactive setup installs llama.cpp, fills the Hub cache, and healthchecks endpoints unless you answered no (or passed `--skip-download`). Leave the token prompt blank only for fully public models.

### Model download and llama.cpp

After the token prompt, `mobyai setup` asks **Install llama.cpp, download models, and test endpoints now?** (default yes). That:

1. Installs the official `llama` binary from https://llama.app if `llama` / `llama-server` is not already on PATH (`~/.llama-app/llama` and `~/.local/bin/llama` are also checked).
2. Downloads each unique `org/name:QUANT` GGUF spec into the Hub cache (`huggingface_hub.snapshot_download`).
3. Spawns each managed role once, waits for `/v1/models`, then stops those test processes.

`mobyai start` then healthchecks first. A live endpoint is reused; otherwise it spawns `llama serve -hf {model}`.

`--non-interactive` skips provision unless you pass `--download-models`. `--skip-download` skips install, download, and the endpoint test. `--skip-runtime` / `--skip-endpoint-test` skip those steps only.

### 2.3 How MobyAI finds the file

First match wins:

1. `mobyai --config PATH …`
2. Environment variable `MOBYAI_CONFIG`
3. `./config.yaml` (current working directory)
4. `./config/default.yaml`
5. `~/MobyAI/config.yaml`

`mobyai setup` writes `./config.yaml` by default so it wins without extra flags. Pin it anyway:

```bash
export MOBYAI_CONFIG="$PWD/config.yaml"
mobyai config validate
```

### 2.4 Create data directories

The wizard does this unless you pass `--no-directories`. By hand:

```bash
mkdir -p ~/MobyAI/projects
mkdir -p "$(dirname ~/MobyAI/mobyai.db)"
```

The SQLite file itself is created on first `mobyai init` or `mobyai doctor`.

---

## 3. What every important YAML field means

Full schema is `AppConfig` in `src/mobyai/models/config.py`. Unknown keys are **rejected**.

### `project.workspace_root`

Directory that will contain one subdirectory per project (unless `mobyai init --workspace` points elsewhere). The harness will not write outside a project workspace.

### `persistence.database_path`

SQLite file. Pause, crash, and `mobyai resume` reload **this file**, not a chat transcript.

### `huggingface.token_file`

Path to a file containing the Hub token (one line). Setup default: `~/MobyAI/secrets/hf_token`. The token string itself is forbidden in YAML.

### `services.mode`

| Value | Who starts the HTTP servers | Who stops them |
|-------|-----------------------------|----------------|
| `external` (default) | You | You |
| `managed` | `mobyai start` / `run` using each role’s `launch` argv | `mobyai stop` |

`external` only healthchecks `GET {endpoint}/v1/models`. It never spawns a process.

`managed` sets `CUDA_VISIBLE_DEVICES` from `hardware.<role>.device` and runs `launch`. Planner and Worker that share a device are swapped (`swap_shared_device`); they are not loaded at the same time. Orchestrator should use a **different** device (or `cpu`) so it is not in the same CUDA process as the coding model.

### `hardware.<role>.device`

GPU index (`0`, `1`, …) or `cpu`. These numbers are not hard-coded in Python. They must match the physical card you intend. `models.<role>.gpu` is a duplicate hint for display; for `cpu`, the wizard writes `gpu: null`.

### `models.<role>`

| Field | Meaning |
|-------|---------|
| `model` | llama.cpp `-hf` spec (`org/name:QUANT`) or the id `GET /v1/models` returns. Setup defaults: Planner `Qwen/Qwen3-32B-GGUF:Q4_K_M`, Orchestrator `Qwen/Qwen2.5-7B-Instruct-GGUF:Q5_K_M`, Worker `unsloth/Qwen3.5-35B-A3B-GGUF:Q4_K_M`. |
| `endpoint` | Base URL, no `/v1/chat/completions` suffix. Example: `http://127.0.0.1:9800` |
| `provider` | Keep `openai_compatible` unless you add another client |
| `engine` | Label only (`llama.cpp`, vllm, ollama, …). Not an imported SDK |
| `inference.max_model_len` | llama.cpp `--ctx-size` via `{ctx}`. Defaults: Planner 32768, Orchestrator 16384, Worker 65536 (Worker packer uses 48000). |
| `launch` | Argv used **only** in managed mode. Tokens `{model}`, `{port}`, and `{ctx}` are substituted. Includes flash attention, q8_0 KV cache, and `--parallel 1` so one slot gets the full window. |

Default ports in the bundled examples:

| Role | Port |
|------|------|
| Worker | 9800 |
| Orchestrator | 9801 |
| Planner | 9802 |

You may change them; keep `endpoint` and managed `launch --port` in sync (`{port}` is taken from the endpoint).

### `runtime` and `context`

Leave the defaults until you have a reason. `max_task_attempts: 2` is the product rule (retry once, then escalate). Do not raise it to “keep trying forever.”

---

## 4. Start inference (before a live `run`)

### External mode

Start three servers yourself, one CUDA process per architecture generation. Example shape (replace binary, weights, and ports with yours):

```bash
CUDA_VISIBLE_DEVICES=1  <orchestrator-server> --port 9801 --model YOUR_ORCHESTRATOR_ID
CUDA_VISIBLE_DEVICES=0  <planner-or-worker-server> --port 9800 --model YOUR_WORKER_ID
```

Planner and Worker may share GPU 0 **over time**, not as two residents in one process if you only have that card. External mode does not swap for you; you must not load both at once on a card that cannot hold them.

Then:

```bash
mobyai --config ./config.yaml config validate
mobyai --config ./config.yaml models
mobyai --config ./config.yaml doctor
mobyai --config ./config.yaml start
```

`doctor` keeps going after a single failure so you see the whole board. Missing `nvidia-smi` is a **warning** in external mode and a **failure** in managed mode.

### Managed mode

1. Put `llama` on PATH (setup does this via https://llama.app) or keep the absolute path setup wrote.
2. Set `services.mode: managed` (or `mobyai setup --mode managed`).
3. `mobyai start` healthchecks each role. Live servers are reused; missing ones are spawned with `llama serve`. Planner and Worker that share a GPU still swap during `run`.

---

## 5. First project

```bash
mobyai --config ./config.yaml init \
  --name demo \
  --objective "Create a small REST API with an SQLite-backed task manager."

mobyai --config ./config.yaml run demo
mobyai --config ./config.yaml status demo
mobyai --config ./config.yaml tasks demo
mobyai --config ./config.yaml logs demo
```

`init` without `--objective` only ensures config, database parent, and workspace root exist.

`run` plans when the project is `NEW`, then executes the DAG. `pause` / `resume` / `stop`:

```bash
mobyai --config ./config.yaml pause demo
mobyai --config ./config.yaml resume demo
mobyai --config ./config.yaml stop demo
```

`resume` reloads SQLite (`resume_project`) and continues the harness. It does not reconstruct history from chat.

## Operator console (settings + monitor)

```bash
mobyai --config ./config.yaml ui
# http://127.0.0.1:8743
```

**Settings** — per role: endpoint, model id, temperature, top-p, top-k. Apply writes `config.overlay.yaml` next to the main YAML (comments in the main file are kept). A running `mobyai run` reloads that overlay on the next model call.

**Monitor** — the whole DAG on one screen. Each step is a collapsible panel. Non-failed steps show the last 100 event/log lines. Failed steps open with the full event + attempt workflow for that step.

`--no-browser` skips opening a tab. `--port 8743` / `--host 127.0.0.1` bind the stdlib HTTP server.

---

## 6. Tests (no GPU)

```bash
source .venv/bin/activate
pytest tests/unit tests/integration
```

The integration suite uses scripted Planner/Worker/Orchestrator payloads under `tests/fixtures/task_manager/`. It does not start vLLM.

---

## 7. If something fails

| Symptom | What to check |
|---------|----------------|
| `No configuration file found` | Pass `--config`, or run `mobyai setup`, or `export MOBYAI_CONFIG=` |
| `Invalid configuration` | Unknown YAML key, missing `models.planner`, or bad types. `mobyai config validate` |
| `doctor` FAIL on an endpoint | Server not listening, wrong port, or TLS vs http |
| `doctor` warn `hf token` | Paste a token in setup, `export HF_TOKEN=...`, or write `~/MobyAI/secrets/hf_token` |
| Gated Hub 401 on setup download | Token file empty/wrong; the Hugging Face token needs repo access |
| Gated Hub 401 on start | Weights missing from the Hub cache; re-run setup and accept the download, or set `HF_TOKEN` |
| Wrong model id | `GET http://127.0.0.1:9800/v1/models` and pass `--worker-model` (or the planner/orchestrator flags) |
| Managed spawn fails | `launch[0]` not on PATH; `nvidia-smi` missing; device index wrong |
| Workspace FAIL | `project.workspace_root` not writable |
| Python too old | Recreate `.venv` with 3.12+ |
| `setup` says stdin is not a terminal | Use `--non-interactive` or `./scripts/setup.sh` from a real TTY |

Read [`README.md`](../README.md) for day-to-day commands after this file is done.

## Long model responses

`runtime.inference_timeout_seconds` defaults to `600` (10 minutes) for all agent roles.
The current client waits for a complete, non-streamed response, so a model can be
actively generating while the client waits. Increase this setting if legitimate
responses need more time. This is an HTTP timeout per attempt; transport retries
can make the total wait longer. It does not change command execution timeouts.

Existing installations should set this value in their active configuration file;
an explicitly configured shorter timeout still takes precedence over the default.
Restart MobyAI after the current run is idle to apply the change.

# Set up MobyAI

This is the operator walkthrough. Do these steps in order. The same sequence is automated by [`scripts/setup.sh`](../scripts/setup.sh).

MobyAI is not a chat app. After setup you will have:

1. A Python package (`mobyai`) on your PATH.
2. A YAML file that names **where state lives** and **which OpenAI-compatible URLs** the Planner, Orchestrator, and Worker use.
3. Empty directories for project workspaces.

The harness will **not** download weights, pick GPUs for you, or invent model ids. Those stay in the YAML.

---

## 0. What you need before starting

Required:

- A checkout of this repository
- **Python 3.12 or newer** (`python3 --version`)
- `pip` (comes with the venv the script creates)
- Disk write access for the workspace root and the SQLite path (defaults under `~/MobyAI/`)

Required later, before `mobyai run` against real models:

- Three OpenAI-compatible HTTP servers (Planner, Orchestrator, Worker), **or** a launchable engine on PATH if you choose `services.mode: managed`
- Model ids that those servers actually serve (`GET /v1/models`)

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
| 5 | Runs `python -m mobyai setup` | Answer prompts (or accept `[defaults]`) |
| 6 | Prints doctor / first-project commands | Start servers, then run those commands |

Non-interactive (CI or “just write a default config.yaml here”):

```bash
./scripts/setup.sh --yes --output ./config.yaml --force
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

**Wizard (prompts for every role):**

```bash
mobyai setup
# writes ./config.yaml unless you pass --output PATH
```

**Wizard, no prompts:**

```bash
mobyai setup --non-interactive --output ./config.yaml --force \
  --mode external \
  --workspace ~/MobyAI/projects \
  --database ~/MobyAI/mobyai.db \
  --planner-model YOUR_PLANNER_ID \
  --planner-endpoint http://127.0.0.1:9802 \
  --planner-device 0 \
  --orchestrator-model YOUR_ORCHESTRATOR_ID \
  --orchestrator-endpoint http://127.0.0.1:9801 \
  --orchestrator-device 1 \
  --worker-model YOUR_WORKER_ID \
  --worker-endpoint http://127.0.0.1:9800 \
  --worker-device 0
```

**Copy a template instead of the wizard:**

```bash
mkdir -p ~/MobyAI
cp config/default.yaml ~/MobyAI/config.yaml          # external servers (default)
# or
cp config/examples/managed.yaml ~/MobyAI/config.yaml # MobyAI spawns vLLM
# or
cp config/examples/dual_gpu.yaml ~/MobyAI/config.yaml
```

Then open the file and replace every `configurable-*-model` string.

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
| `model` | Id the OpenAI-compatible server expects. Must match `GET /v1/models`. Placeholders like `configurable-coding-model` will fail at runtime. |
| `endpoint` | Base URL, no `/v1/chat/completions` suffix. Example: `http://127.0.0.1:9800` |
| `provider` | Keep `openai_compatible` unless you add another client |
| `engine` | Label only (vllm, ollama, …). Not an imported SDK |
| `launch` | Argv used **only** in managed mode. Tokens `{model}` and `{port}` are substituted |

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

1. Put the first token of each `launch` list on PATH (the examples use `vllm`).
2. Set `services.mode: managed` (or `mobyai setup --mode managed`).
3. `mobyai start` brings the Orchestrator up and leaves Planner/Worker to swap during `run`.

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
| Placeholder model id | `GET http://127.0.0.1:9800/v1/models` and paste the real id |
| Managed spawn fails | `launch[0]` not on PATH; `nvidia-smi` missing; device index wrong |
| Workspace FAIL | `project.workspace_root` not writable |
| Python too old | Recreate `.venv` with 3.12+ |
| `setup` says stdin is not a terminal | Use `--non-interactive` or `./scripts/setup.sh` from a real TTY |

Read [`README.md`](../README.md) for day-to-day commands after this file is done.

# MobyAI

MobyAI is a local-first agentic software engineering harness. It runs long-horizon coding projects on consumer hardware using three specialized roles (Planner, Orchestrator, Worker) behind a deterministic application that owns state, retries, tests, and files.

This is not a chat-based coding assistant. Language models propose structured actions. The Python harness is the authority: it stores project state, enforces a state machine, validates schemas, executes tests, and decides what happens next.

## Current status

Phase 1–12 are complete (domain model, SQLite, execution, inference, Planner, ContextPacker, Worker, Orchestrator, DAG-branch replan, GPU service manager, CLI, end-to-end fixture). The MVP harness is internally consistent. Live GPU runs are operator-side.

## Setup (do this first)

The full walkthrough — prerequisites, venv, every YAML field, external vs managed servers, doctor, and first project — is **[docs/setup.md](docs/setup.md)**.

From the repository root, run the script. It pauses after each step:

```bash
./scripts/setup.sh
```

That script finds Python 3.12+, creates `.venv`, installs `mobyai`, then runs `mobyai setup` so you can set workspace, database, service mode, model ids, endpoints, and GPU devices.

Non-interactive:

```bash
./scripts/setup.sh --yes --output ./config.yaml --force
```

Equivalent without the script:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
mobyai setup
```

Then `source .venv/bin/activate` in every new shell.

## Layout

- [`docs/setup.md`](docs/setup.md) — operator install and config walkthrough
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — living architecture and phase tracker
- [`docs/implementation-notes.md`](docs/implementation-notes.md) — design decisions per subsystem
- [`scripts/setup.sh`](scripts/setup.sh) — interactive first-run script
- [`config/default.yaml`](config/default.yaml) — reference configuration (external endpoints)
- [`config/examples/dual_gpu.yaml`](config/examples/dual_gpu.yaml) — dual-GPU device map
- [`config/examples/managed.yaml`](config/examples/managed.yaml) — managed-process launch example
- [`src/mobyai/`](src/mobyai/) — Python package
- [`tests/fixtures/task_manager/`](tests/fixtures/task_manager/) — Phase 12 REST+SQLite fixture (scripted models)

## Prerequisites

- Python 3.12+
- Local OpenAI-compatible inference servers before a live `run` (default `services.mode: external`)
- Optional NVIDIA GPUs for managed-process mode; unit and integration tests do not require GPUs

## Configuration

`mobyai setup` writes a commented `config.yaml`. You can also copy [`config/default.yaml`](config/default.yaml) and replace every `configurable-*-model` id with a value from `GET /v1/models`.

`--config PATH` overrides the search order: `MOBYAI_CONFIG`, `./config.yaml`, `./config/default.yaml`, `~/MobyAI/config.yaml`.

Validate with:

```bash
mobyai --config ./config.yaml config validate
mobyai --config ./config.yaml models
```

## Starting inference servers

External mode (default) expects Planner, Orchestrator, and Worker OpenAI-compatible servers already listening on the configured ports. Check them with:

```bash
mobyai --config ./config.yaml doctor
mobyai --config ./config.yaml start
```

Managed mode (`config/examples/managed.yaml`, or `mobyai setup --mode managed`) lets MobyAI spawn isolated processes with `CUDA_VISIBLE_DEVICES` from `hardware`. Planner and Worker that share a device are swapped; they are not loaded at the same time.

## Creating and running a project

```bash
mobyai --config ./config.yaml init --name demo --objective "Create a small REST API with an SQLite-backed task manager."
mobyai --config ./config.yaml run demo
mobyai --config ./config.yaml status demo
mobyai --config ./config.yaml tasks demo
mobyai --config ./config.yaml logs demo
```

`run` plans if the project is `NEW`, then executes the DAG. `status` shows current task, progress, last error, attempt count, active worker model, and GPU.

## Pausing and resuming

```bash
mobyai --config ./config.yaml pause demo
mobyai --config ./config.yaml resume demo
mobyai --config ./config.yaml stop demo
```

`pause` stores `paused_from` so resume is deterministic. `resume` reloads SQLite (`resume_project`) and continues the harness; it does not reconstruct history from a chat transcript. `stop` pauses the project and stops managed inference processes.

## Tests

```bash
pytest tests/unit tests/integration
```

The integration suite runs the fixture task manager through plan, retry, escalate, DAG patch, SQLite reconnect, and CLI `run`. It does not start GPUs or vLLM.

## Troubleshooting

- [`docs/setup.md`](docs/setup.md) — install failures, config search order, placeholder model ids
- `mobyai doctor` — config, workspace writes, database, endpoint health, GPU, launch argv
- `mobyai config validate` — YAML schema only
- Failed tasks stay in the DAG; first failure retries, second failure escalates to a branch patch
- Path escapes, destructive commands, and chat-dumped source are rejected by the harness, not by the model

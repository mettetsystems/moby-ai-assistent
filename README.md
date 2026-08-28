# MobyAI

MobyAI is a local-first agentic software engineering harness. It runs long-horizon coding projects on consumer hardware using three specialized roles (Planner, Orchestrator, Worker) behind a deterministic application that owns state, retries, tests, and files.

This is not a chat-based coding assistant. Language models propose structured actions. The Python harness is the authority: it stores project state, enforces a state machine, validates schemas, executes tests, and decides what happens next.

## Current status

Phase 1–12 are complete (domain model, SQLite, execution, inference, Planner, ContextPacker, Worker, Orchestrator, DAG-branch replan, GPU service manager, CLI, end-to-end fixture). The MVP harness is internally consistent. Live GPU runs are operator-side.

## Layout

- [`docs/implementation-plan.md`](docs/implementation-plan.md) — living architecture and phase tracker
- [`docs/implementation-notes.md`](docs/implementation-notes.md) — design decisions per subsystem
- [`config/default.yaml`](config/default.yaml) — reference configuration (external endpoints)
- [`config/examples/dual_gpu.yaml`](config/examples/dual_gpu.yaml) — dual-GPU device map
- [`config/examples/managed.yaml`](config/examples/managed.yaml) — managed-process launch example
- [`src/mobyai/`](src/mobyai/) — Python package
- [`tests/fixtures/task_manager/`](tests/fixtures/task_manager/) — Phase 12 REST+SQLite fixture (scripted models)

## Prerequisites

- Python 3.12+
- Local OpenAI-compatible inference servers (default `services.mode: external`)
- Optional NVIDIA GPUs for managed-process mode; unit tests do not require GPUs

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

Copy [`config/default.yaml`](config/default.yaml) and point model IDs, endpoints, and device indices at your machines. `--config PATH` overrides the search order: `MOBYAI_CONFIG`, `./config.yaml`, `./config/default.yaml`, `~/MobyAI/config.yaml`.

Validate with:

```bash
mobyai --config config/default.yaml config validate
```

## Starting inference servers

External mode (default) expects Planner, Orchestrator, and Worker OpenAI-compatible servers already listening on the configured ports. Check them with:

```bash
mobyai doctor
mobyai start
mobyai models
```

Managed mode (`config/examples/managed.yaml`) lets MobyAI spawn isolated processes with `CUDA_VISIBLE_DEVICES` from `hardware`. Planner and Worker that share a device are swapped; they are not loaded at the same time.

## Creating and running a project

```bash
mobyai init --name demo --objective "Create a small REST API with an SQLite-backed task manager."
mobyai run demo
mobyai status demo
mobyai tasks demo
mobyai logs demo
```

`run` plans if the project is `NEW`, then executes the DAG. `status` shows current task, progress, last error, attempt count, active worker model, and GPU.

## Pausing and resuming

```bash
mobyai pause demo
mobyai resume demo
mobyai stop demo
```

`pause` stores `paused_from` so resume is deterministic. `resume` reloads SQLite (`resume_project`) and continues the harness; it does not reconstruct history from a chat transcript. `stop` pauses the project and stops managed inference processes.

## Tests

```bash
pytest tests/unit tests/integration
```

The integration suite runs the fixture task manager through plan, retry, escalate, DAG patch, SQLite reconnect, and CLI `run`. It does not start GPUs or vLLM.

## Troubleshooting

- `mobyai doctor` — config, workspace writes, database, endpoint health, GPU, launch argv
- `mobyai config validate` — YAML schema only
- Failed tasks stay in the DAG; first failure retries, second failure escalates to a branch patch
- Path escapes, destructive commands, and chat-dumped source are rejected by the harness, not by the model

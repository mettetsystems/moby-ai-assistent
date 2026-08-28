# MobyAI Implementation Plan

Living architecture and phase tracker. The original master specification prompt is kept locally as `docs/build-plan.md` and is gitignored. Update this file at the end of every phase.

**Current phase:** 12 complete — MVP harness is internally consistent

---

## 1. Repository assessment

The repository was greenfield at Phase 0:

| Item | Status at Phase 0 |
|------|-------------------|
| Git | Single `Initial commit` on `main` |
| README | One-line title `# moby-ai-assistent` |
| `docs/build-plan.md` | Copy of the master specification prompt, not a derived plan |
| Python package | Absent |
| Tests | Absent |
| Config / CLI / SQLite | Absent |

Nothing reusable existed. There was no competing architecture to migrate. The git remote and repo name (`moby-ai-assistent`) are kept; the installable package is `mobyai`.

`docs/build-plan.md` is **not overwritten** and is gitignored so the prompt is not committed. This file is the durable build plan the spec asked for.

---

## 2. Proposed final architecture

MobyAI is a single-user local workflow engine. Three LLM roles provide intelligence; the harness provides discipline.

```text
USER
  │
  ▼
PLANNER  ──►  validated Task DAG  ──►  SQLite (app-owned state)
                                        │
                                        ▼
                                  DAG scheduler
                                        │
                                        ▼
                                  ORCHESTRATOR
                                        │
                                        ▼
                                     WORKER
                                        │
                                        ▼
                              filesystem + test runner
                                        │
                          ┌─────────────┴─────────────┐
                          PASS                    FAIL
                           │                        │
                           ▼                        ▼
                      next ready task      retry (1) or escalate (2)
```

Principles:

- Models never own execution state. They receive serialized projections and return structured contracts.
- Application code owns counters, DAG eligibility, process exit codes, schema validation, and state transitions.
- Planner and Worker may share a physical GPU over time, but not a CUDA process with the Orchestrator, and not necessarily simultaneous residency.
- Inference is an OpenAI-compatible HTTP client. vLLM is one possible engine behind that interface, not an application dependency.

Target package layout (later phases fill empty packages):

```text
src/mobyai/
  models/          domain + agent contracts
  engine/          harness, state machine, scheduler, dispatcher, recovery
  dag/             graph + validator
  state/           repository protocols + SQLite + in-memory
  inference/       provider protocol, OpenAI-compatible client, GPU service manager
  context/         packer, tokenizer, log reducer
  execution/       commands, filesystem, patches, tests
  agents/          planner, orchestrator, worker + prompts
  observability/   structured logs and events
  cli.py
```

---

## 3. Dependency choices

Keep the dependency set small. Standard library first.

| Need | Choice | Phase |
|------|--------|-------|
| Domain validation | Pydantic v2 | 1 |
| Config files | PyYAML + `AppConfig.from_yaml` | 1 |
| Tests | pytest + pytest-asyncio | 1 |
| Packaging | hatchling, `src/` layout, Python >= 3.12 | 1 |
| Persistence | stdlib `sqlite3` behind Protocols (async wrappers as needed) | 2 |
| HTTP inference | `httpx` | 4 |
| Token budgets | lightweight counter first; tokenizer library only if accuracy requires it | 6 |

Not in MVP: FastAPI, Redis, Kubernetes, ORMs, vector databases, cloud SDKs.

---

## 4. Module boundaries

| Package | Owns | Must not own |
|---------|------|----------------|
| `models` | Enums, Pydantic contracts, config schema | I/O, inference, scheduling |
| `engine` | State transitions, later harness loop | Filesystem writes, model HTTP |
| `dag` | Cycle checks, ready/blocked selection | LLM prompts |
| `state` | Persistence Protocols and implementations | Business transition rules |
| `execution` | Workspace-safe files and commands | Agent policy |
| `inference` | Providers and GPU process lifecycle | Task DAG |
| `agents` | Prompt assembly and contract parsing | Authoritative state |
| `context` | Worker payload construction under a token budget | Persistence |
| `observability` | Structured event logs | Control flow |

Dependency direction: `cli` → `engine` → (`agents`, `dag`, `state`, `execution`, `inference`, `context`) → `models`. Lower layers must not import the harness.

---

## 5. Database / schema design

Phase 1 defined Protocols and an in-memory store. Phase 2 implemented SQLite with this schema (JSON columns stored as TEXT):

**projects** — `project_id` PK, `name`, `objective`, `workspace_root`, `state`, `paused_from`, `architecture_summary`, `directory_structure`, `documentation_requirements`, `current_task_id`, `last_error`, `created_at`, `updated_at`

**tasks** — PK `(project_id, task_id)`, task payload fields, `status`, `attempt_count`, `superseded_by`

**task_attempts** — PK identity, `(project_id, task_id, attempt_number)` unique, timestamps, worker response, test exit code, log path, result

**checkpoints** — `checkpoint_id` PK, `project_id`, `kind`, `created_at`, `payload`

**events** — append-only structured log rows for observability

Repositories:

- `ProjectRepository.get/save/list_ids`
- `TaskRepository.get/list_for_project/save/save_all`
- `CheckpointRepository.save/latest/list`
- `AttemptRepository.save/get/list_for_task/list_for_project`
- `EventRepository.append/list_for_project`
- `StateStore.load_snapshot` / `resume_project` reconstructs a `ProjectSnapshot` after restart

SQLite is an implementation, not the interface. WAL mode and a single writer are sufficient for one local user.

---

## 6. State-machine design

`ProjectState`: `NEW`, `PLANNING`, `PLAN_READY`, `READY`, `DISPATCHING`, `WORKING`, `VALIDATING`, `RETRYING`, `ESCALATING`, `REPLANNING`, `PAUSED`, `FAILED`, `COMPLETED`.

Allowed transitions (application-enforced):

```text
NEW → PLANNING
PLANNING → PLAN_READY | FAILED
PLAN_READY → READY
READY → DISPATCHING | COMPLETED | FAILED
DISPATCHING → WORKING
WORKING → VALIDATING | FAILED
VALIDATING → READY | RETRYING | ESCALATING
RETRYING → DISPATCHING
ESCALATING → REPLANNING | FAILED
REPLANNING → READY | FAILED
non-terminal except PAUSED → PAUSED (store paused_from)
PAUSED → paused_from
FAILED and COMPLETED are terminal
```

Tightening versus the spec diagram:

- `RETRYING` returns to `DISPATCHING` so a retry re-runs the Worker.
- `VALIDATING` may go to `ESCALATING` when `attempt_count >= max_task_attempts` (default 2).
- Pause stores `Project.paused_from` so resume is deterministic after a crash.

Task-level states are separate (`pending`, `ready`, `running`, `testing`, `passed`, `failed`, `blocked`, `superseded`). DAG code, not the LLM, decides `ready` vs `blocked`. `is_complete` ignores `SUPERSEDED` nodes so a patched branch can still finish.

---

## 7. Agent contracts

All agent I/O is Pydantic-validated. Malformed output is rejected. Bounded schema-repair is Phase 4.

**PlannerOutput** — `project_name`, `architecture_summary`, `directory_structure`, `tasks[]` (atomic specs with files, deps, acceptance criteria, tests), `documentation_requirements`. The Planner does not implement code or mark tests passed.

**DAGPatch** — escalation-only contract: `summary`, optional `architecture_summary` / extra `directory_structure`, `supersede[]` (failed-branch nodes → new `PlannerTaskSpec` replacements), optional `add_tasks`. The Planner must not emit a full `PlannerOutput` on replan. PASSED tasks are never superseded.

**OrchestratorResult** — `decision_summary`, `decision` in `{CONTINUE, RETRY_WITH_FIX, ESCALATE_TO_PLANNER, MISSION_COMPLETE, BLOCKED}`, optional `task_id` and `next_payload`. Counters, exit codes, and eligible-task selection stay in application code.

**WorkerResult** — `summary`, structured `tool_calls` (`read_file`, `write_file`, `apply_patch`, `list_directory`, `search_code`, `run_test`, `run_command`), `done`, `changed_files`. The Worker never writes files by dumping source into chat.

---

## 8. Testing strategy

GPUs are never required for the default unit suite. Inference is mocked from Phase 4 onward.

| Area | Coverage |
|------|----------|
| State machine | Valid/invalid transitions, pause/resume, failure, completion |
| DAG | Ready selection, blocked dependents, cycles, missing deps, unlock after pass |
| Contracts | Valid/invalid Planner, Orchestrator, Worker payloads; schema-repair |
| Config | YAML load, `~` expansion, extra fields rejected |
| Persistence | Save/reload; SQLite file survives reconnect; interrupted-task resume |
| Execution | Workspace escape, timeouts, large output, patches, log reduction |
| Context | Token budget, target/import/test inclusion, log truncation, unrelated-history exclusion |
| Worker | Tool dispatch through the jail, chat-dump ignored, path escape, harness tests vs `done` |
| Recovery | Fail #1 retry, fail #2 escalate (Phase 8); DAG-branch replan (Phase 9) |
| GPU services | External healthcheck; managed spawn with config `CUDA_VISIBLE_DEVICES`; same-device swap (Phase 10) |
| CLI | init/setup/ui/doctor/start/run/status/pause/resume/stop/tasks/logs/models/config validate |
| End-to-end | Fixture REST+SQLite task manager: plan, fail/retry/escalate, DAG patch, SQLite reconnect, CLI run (Phase 12) |

Each phase runs its new tests, then the broader unit suite, before the next phase starts.

---

## 9. Implementation phases

| Phase | Name | Status |
|-------|------|--------|
| 0 | Repository analysis + this plan | complete |
| 1 | Domain model, state machine, DAG, persistence Protocols | complete |
| 2 | SQLite persistence, checkpoints, resume | complete |
| 3 | Workspace, files, command/test runner, logs | complete |
| 4 | Inference provider, structured parse, bounded repair | complete |
| 5 | Planner loop (no Worker) | complete |
| 6 | ContextPacker | complete |
| 7 | Worker + safe artifact application | complete |
| 8 | Orchestrator decisions + retry accounting | complete |
| 9 | Escalation / DAG branch replan | complete |
| 10 | GPU service manager (external endpoints first) | complete |
| 11 | CLI | complete |
| 12 | End-to-end fixture project | complete |

Phase 1 delivered: `src/mobyai` package, YAML `AppConfig`, project/task/checkpoint/agent Pydantic models, `ProjectStateMachine`, `TaskGraph` + `validate_dag`, async persistence Protocols with in-memory stores, and unit tests. `docs/build-plan.md` remains a local gitignored copy of the master spec.

Phase 2 delivered: versioned SQLite schema (`projects`, `tasks`, `task_attempts`, `checkpoints`, `events`), WAL + foreign keys, `SqliteStateStore` behind the same Protocols, attempt/event history, `resume_project` snapshot load after reconnect, and `persistence.database_path` in config.

Phase 3 delivered: `Workspace` path jail (traversal, absolute escape, symlink escape, optional extra allowed roots), `WorkspaceFilesystem` (read/write/list/search, atomic writes), unified `apply_patch`, `CommandRunner` (`argv` only, timeouts, output caps, destructive-command denylist), `TestRunner` (exit code is truth), `AttemptLogStore` under `artifacts/logs/<task>/attempt_NNN.log`, and `LogReducer` for model-facing head/tail/traceback/failed tests. Worker tool dispatch is Phase 7.

Phase 4 delivered: `InferenceProvider` protocol, `OpenAICompatibleProvider` (`/v1/chat/completions`, `/v1/models` health), `StructuredGenerator` with JSON extraction (fences/prose) and bounded schema repair, `FakeInferenceProvider` plus httpx `MockTransport` tests (no GPU). vLLM/Ollama/SGLang are config `provider` aliases, not SDKs.

Phase 5 delivered: `PlannerAgent` (structured plan + deterministic DAG/atomicity/path checks + bounded plan repair), `plan_project` (NEW/PLANNING → READY, or FAILED), `PLAN_CREATED` checkpoint, workspace tree inspection, directory scaffolding without writing app files. Worker is not invoked.

Phase 6 delivered: `ContextPacker.build_worker_context` packs a `WorkerContext` under `context.worker_token_budget` (char/4 estimator, no tokenizer library). Payload is objective, atomic instruction, target files, one-hop local imports, required context, tests, acceptance criteria, test commands, constraints, and a `LogReducer` view of the latest failed attempt. Unrelated files, other tasks, and `worker_response` history are excluded. Worker invocation remains Phase 7.

Phase 7 delivered: `WorkerAgent` returns structured `tool_calls` only; `ToolDispatcher` applies them through `WorkspaceFilesystem` / `apply_patch` / `CommandRunner` (path jail, destructive denylist, timeouts). `execute_task` packs context, runs a bounded tool loop (`runtime.max_worker_turns`), then the harness runs `test_commands`. Pass requires files on disk and process exit 0 — Worker `done` is not authority. Success: VALIDATING → READY, task PASSED. Failure: project stays VALIDATING for retry/escalate. Chat dumps do not write files.

Phase 8 delivered: `advance_project` / `run_project` select the next DAG-ready task (not the LLM), dispatch `execute_task`, then evaluate. First failure → RETRYING with an Orchestrator `RETRY_WITH_FIX` instruction; second failure → ESCALATING with an `EscalationPackage` (task FAILED, dependents BLOCKED). `MISSION_COMPLETE` is DAG `is_complete()`, not a model vote. The Orchestrator cannot override counters or pick a non-eligible task.

Phase 9 delivered: `PlannerAgent.replan` returns a `DAGPatch` (not a full `PlannerOutput`). `apply_dag_patch` supersedes the failed branch, retargets dependents onto replacement sinks, and refuses PASSED or out-of-branch nodes. `replan_project` persists ESCALATING → REPLANNING before inference, then REPLANNING → READY (or FAILED). `run_project(..., planner=)` continues after a successful patch; without a planner it still stops at ESCALATING.

Phase 10 delivered: `GpuServiceManager` implements `InferenceServiceManager`. Default `services.mode` is `external` (healthcheck the configured endpoint, never spawn). `managed` starts argv-only processes with `CUDA_VISIBLE_DEVICES` from `hardware.for_role`, not hard-coded GPU indices. Planner and Worker sharing a device persist-then-swap (`swap_shared_device`). Engine loops take optional `services=` and call `ensure_role_available` after state is persisted. Unit tests use fake processes; GPUs are not required.

Phase 11 delivered: `mobyai` CLI (`python -m mobyai` / console script) with `init`, `doctor`, `start`, `run`, `status`, `pause`, `resume`, `stop`, `tasks`, `logs`, `models`, and `config validate`. `Runtime` wires config, SQLite, agents, and the GPU manager. `resume` loads `resume_project` then continues the harness; it does not ask a model what the current task was. Tests inject fakes; GPUs are not required.

Phase 12 delivered: fixture objective “Create a small REST API with an SQLite-backed task manager.” Scripted Planner/Worker/Orchestrator payloads in `tests/fixtures/task_manager/` drive `tests/integration/test_task_manager.py` through planning, Worker execution, harness tests, fail #1 retry, fail #2 escalate, DAG patch, continuation, and COMPLETED. SQLite reconnect after the first passed task continues without reconstructing history from chat. A CLI `init`/`run` path covers the same fixture. No GPU required.

Operator setup (post-MVP): `./scripts/setup.sh` walks Python 3.12+, `.venv`, `pip install -e ".[dev]"`, then `mobyai setup`. Defaults are a Hugging Face token file, managed vLLM on two GPUs, and reference Qwen model ids; every prompt still accepts a custom value. `docs/setup.md` is the same sequence written for a human.

---

## 10. Identified technical risks

1. **GPU 0 multiplexing** — Planner and Worker likely cannot stay loaded together on one 5090-class card. Mitigation: `GpuServiceManager` persist-then-swap when roles share a configured device.
2. **Mixed CUDA generations** — 2070 Super and 5090 must not share a process. Mitigation: config-driven `CUDA_VISIBLE_DEVICES` isolation from day one; no hard-coded device numbers.
3. **Malformed model JSON** — Mitigation: Pydantic rejection plus bounded repair (`StructuredGenerator`, Phase 4).
4. **Log / context overflow** — Mitigation: full logs on disk, `LogReducer` in prompts, worker token budget (Phases 3 and 6).
5. **Crash mid-transition** — Mitigation: persist before and after every meaningful transition (Phase 2).
6. **Hanging tests** — Mitigation: timeouts and process control in `CommandRunner` (Phase 3).
7. **Planner over-scoping** — Mitigation: DAG validator plus required `test_commands` / `acceptance_criteria` and relative-path checks in `validate_planner_output` (Phase 5).
8. **Path / symlink escape** — Mitigation: workspace root jail in the filesystem layer (Phase 3).
9. **Optimistic Worker claims** — Mitigation: harness-run tests are the only completion signal.
10. **Config drift of model IDs** — Mitigation: names live only in YAML, never scattered as constants.

# Implementation notes

Design record for major subsystems. Each section covers what it does, why it exists, why this design was selected, alternatives, and how it can be replaced.

---

## Phase 1 — Domain kernel

### Configuration (`src/mobyai/models/config.py`)

**What it does.** Loads YAML into a Pydantic `AppConfig`: workspace root, runtime limits, context budgets, per-role model endpoints, and optional hardware device map.

**Why it exists.** Model names, GPU indices, ports, and quantization must not be scattered as constants. Hardware differences (one GPU vs dual GPU, CPU inference) are configuration.

**Why this design.** `AppConfig.from_yaml` plus `extra="forbid"` rejects unknown keys early. `~` in `workspace_root` is expanded at load time. Nested `RoleModelConfig` / `HardwareConfig` are keyed by agent role so later GPU code can ask `hardware.for_role(role)` without branching on strings.

**Alternatives considered.** pydantic-settings YAML sources (more magic, weaker “one file in, one object out”); TOML (fine, but the spec asked for YAML); environment-variable-only config (hostile for multi-model local setups).

**How to replace.** Keep `AppConfig` as the in-memory contract. A future loader can merge `default.yaml` + user file + env overlays without changing consumers.

### Domain models and agent contracts (`src/mobyai/models/`)

**What they do.** Define `Project`, `Task`, `Checkpoint`, and the Planner / Orchestrator / Worker JSON contracts.

**Why they exist.** Models must not own state. Structured I/O is the only way agents talk to the harness. Invalid JSON is a hard failure in Phase 1.

**Why this design.** Pydantic v2 with `extra="forbid"` and `StrEnum` values matching the spec. Worker output is tool calls, not file bodies in prose. `PlannerTaskSpec.to_task()` is the only conversion into runtime `Task` (which adds `status` / `attempt_count`).

**Alternatives considered.** Dataclasses + hand-rolled JSON (more code, worse errors); a single mega-schema (harder to evolve per agent); allowing extra fields (hides prompt/contract drift).

**How to replace.** Add fields with defaults. Bounded schema-repair in Phase 4 wraps `model_validate`; it should not fork a second schema language.

### Project state machine (`src/mobyai/engine/state_machine.py`)

**What it does.** Allows only the documented `ProjectState` transitions. Pause stores `paused_from`; resume restores it. `next_after_validation_failure` maps attempt counts to `RETRYING` vs `ESCALATING`.

**Why it exists.** An LLM claiming “we are done” must not change project state. Crash recovery needs an explicit, testable table.

**Why this design.** Frozen allowed-transition pairs; `pause` / `resume` as first-class operations; retry returns to `DISPATCHING` so the Worker actually runs again. Terminal states (`FAILED`, `COMPLETED`) cannot be paused or left.

**Alternatives considered.** A generic FSM library (unnecessary for one machine); encoding retry vs escalate as Orchestrator decisions (the spec says counters are application-owned); `RETRYING → VALIDATING` as in the spec diagram (would skip re-execution).

**How to replace.** Extend `ALLOWED_TRANSITIONS`. Do not let agents write `project.state`.

### Task DAG (`src/mobyai/dag/`)

**What it does.** Validates uniqueness, dependency existence, and acyclicity. Computes `ready` / `pending` / `blocked`. Selects the next task in stable `task_id` order.

**Why it exists.** Dependency ordering is not an LLM problem. Blocked successors must follow failed or superseded nodes without a prompt.

**Why this design.** Separate `validate_dag` from `TaskGraph` so Planner output can be rejected before persistence. Refresh only mutates `pending` / `ready` / `blocked`, so `running` / `testing` / `passed` / `failed` / `superseded` stay under the harness. Copies on read prevent accidental aliasing.

**Alternatives considered.** NetworkX (heavy); storing edge tables separately (premature); asking the Orchestrator which task is eligible (nondeterministic and redundant).

**How to replace.** `TaskGraph` can later wrap a persisted edge list. The eligibility rules should stay in this module. Branch edits go through `apply_dag_patch` (Phase 9), not through the Orchestrator.

### Persistence interfaces (`src/mobyai/state/`)

**What they do.** `ProjectRepository`, `TaskRepository`, and `CheckpointRepository` Protocols, plus in-memory implementations that deep-copy on save/load.

**Why they exist.** Phase 1 needs save/reload tests without SQLite. Phase 2 must be able to swap the backend without rewriting the harness.

**Why this design.** Async Protocols from the start so SQLite (possibly via `aiosqlite` or a thread wrapper) and the in-memory store share one shape. In-memory copies make isolation bugs visible in tests.

**Alternatives considered.** Implementing SQLite immediately (larger Phase 1, slower feedback); a single god-store class (harder to test); sync-only APIs (would have to break callers when the harness goes async).

**How to replace.** Implemented in Phase 2 as `SqliteStateStore`. Keep `InMemoryStateStore` for unit tests that should not touch disk.

---

## Phase 2 — SQLite persistence

### Schema and migrations (`src/mobyai/state/migrations.py`)

**What it does.** Applies versioned SQL (currently v1) to create `projects`, `tasks`, `task_attempts`, `checkpoints`, `events`, and `schema_migrations`. JSON lists/objects are stored as TEXT.

**Why it exists.** Long-horizon runs must survive process death. A conversation log is not a database.

**Why this design.** Integer schema versions with an applied-at table so later phases can add columns without ad-hoc `CREATE TABLE IF NOT EXISTS` sprawl. Foreign keys keep attempts and checkpoints attached to real projects/tasks.

**Alternatives considered.** SQLAlchemy/ORM (heavier than a single-user local file); dumping Pydantic JSON blobs as one row per project (harder to query task status); skipping migrations until the schema hurts (it always hurts).

**How to replace.** Append `(2, SCHEMA_V2)` to `MIGRATIONS`. Do not edit v1 in place once real databases exist.

### SQLite store (`src/mobyai/state/sqlite.py`)

**What it does.** Implements the Phase 1 Protocols plus attempts and events. WAL mode, `foreign_keys=ON`, `busy_timeout`. Async callers hop to a worker thread under one asyncio lock so stdlib `sqlite3` stays serialized.

**Why it exists.** The harness must persist every meaningful transition before the next agent call. In-memory stores cannot resume after a reboot.

**Why this design.** No `aiosqlite` dependency; the interface is still `async` so the later harness does not block the event loop on disk. Upserts keep `save` idempotent. `PersistenceError` wraps integrity failures (e.g. a task without a project).

**Alternatives considered.** `aiosqlite` (fine, extra dep); a process-wide sync API (would stall inference I/O later); storing checkpoints only and reconstructing tasks from payload JSON (duplicates the tables).

**How to replace.** Swap `SqliteStateStore` for another `StateStore`. Keep `InMemoryStateStore` for tests that should not touch disk.

### Resume (`src/mobyai/engine/recovery.py`)

**What it does.** `resume_project(store, project_id)` loads a `ProjectSnapshot` (project, tasks, attempts, checkpoints). It does not change `ProjectState`. A paused project still has `paused_from`; the state machine resumes it.

**Why it exists.** `mobyai resume` (Phase 11) must reconstruct execution from disk, not from an LLM transcript.

**Why this design.** Persistence loads; the state machine transitions. Mixing those would make “resume” a hidden state change and break crash-mid-WORKING recovery.

**Alternatives considered.** Auto-transitioning PAUSED → `paused_from` inside the store (hides operator intent); rebuilding history from event logs (events are observability, not the source of truth).

**How to replace.** `mobyai resume` calls `resume_project` then, if PAUSED, `ProjectStateMachine.resume`, then the harness loop. Do not ask a model what the current task was.

---

## Phase 3 — Execution system

### Workspace jail (`src/mobyai/execution/workspace.py`)

**What it does.** Resolves every user-supplied path against an explicit project root. After `Path.resolve()` (symlink-following), the real path must sit under the root or a configured extra allowed root.

**Why it exists.** Models hallucinate `../` and absolute paths. A coding harness that writes wherever the model asks is a data-loss bug.

**Why this design.** Check the resolved path, not the string. `extra_allowed_paths` covers the spec’s “additional allowed path” without weakening the default jail. Missing files raise `WorkspaceError` instead of leaking `FileNotFoundError` from `strict=True`.

**Alternatives considered.** chroot/containers (heavy for a single-user MVP); string-prefix checks without `resolve()` (loses to symlinks); allowing all absolute paths that “look safe”.

**How to replace.** Keep `Workspace.resolve` as the only entry for paths. A later sandbox (bubblewrap, Landlock) can wrap the same API.

### Filesystem and patches (`filesystem.py`, `patches.py`)

**What they do.** `read_file` / `write_file` / `list_directory` / `search_code` stay inside the jail. Writes are atomic (`tmp` + replace). `apply_patch` accepts unified diffs, creates/modifies/deletes single files, and rejects non-relative headers.

**Why they exist.** The Worker must not dump file bodies through chat. The harness applies structured tool requests.

**Why this design.** No `shell=True` editor; a small unified-diff applier is testable without GNU `patch`. Directory deletes via patch are refused.

**Alternatives considered.** Calling out to `patch(1)` (platform variance); JSON `{path, contents}` only (weaker for later Worker models that emit diffs).

**How to replace.** Swap the applier; keep the workspace check on every path in the diff.

### Commands, tests, logs (`commands.py`, `tests.py`, `logs.py`, `context/log_reducer.py`)

**What they do.** `CommandRunner` execs an argv list in the workspace, kills the process group on timeout, caps stdout/stderr (head + tail), and denies destructive programs (`rm`, `dd`, `git clean`, …) unless gated. `TestRunner` treats exit code 0 as pass. Full text goes to `artifacts/logs/<task>/attempt_NNN.log`. `LogReducer` extracts head/tail, traceback, pytest `FAILED` names, and `E ` assertion lines for prompts.

**Why they exist.** Tests hang, commands print gigabytes, and models must not see raw logs. Success is a process result, not a Worker sentence.

**Why this design.** argv lists block shell injection. Output caps keep memory bounded while still retaining the tail, where pytest failures live. `__test__ = False` on `TestRunner` so pytest does not collect the class.

**Alternatives considered.** `shell=True` with a regex denylist (injection-prone); unlimited `communicate()` (OOM); streaming every byte to disk with no cap (fills the disk on a runaway test).

**How to replace.** Raise `max_output_bytes` / `max_log_bytes` in YAML. Add allowlists per project later. Do not let the Worker pick `cwd` outside `Workspace`.

---

## Phase 4 — Inference abstraction

### Provider protocol (`src/mobyai/inference/base.py`, `openai_compatible.py`)

**What it does.** `InferenceProvider.generate` / `healthcheck` talk to local OpenAI-compatible HTTP servers. `OpenAICompatibleProvider` posts `/v1/chat/completions` with optional `response_format: json_object` and checks `/v1/models` (then `/health`).

**Why it exists.** Orchestration must not import vLLM. Endpoints, model IDs, and engines live in YAML.

**Why this design.** httpx + the public chat-completions shape covers vLLM, llama.cpp, Ollama, and SGLang. `provider` aliases (`vllm`, `ollama`, …) all construct the same client. The official OpenAI SDK was skipped as extra surface for a local-only MVP. Process start/stop lives in `GpuServiceManager` (Phase 10), not in this HTTP client.

**Alternatives considered.** Engine-specific clients (locks the harness to vLLM); OpenAI Python SDK (cloud-oriented defaults); embedding health in `generate` (hides `mobyai doctor` later).

**How to replace.** Add a new class that satisfies `InferenceProvider` and register it in `provider_from_role_config`. Do not call HTTP from agents directly. Do not start CUDA processes from the provider.

### Structured parse and repair (`structured.py`)

**What it does.** Extracts JSON from raw model text (markdown fences or surrounding prose), validates with Pydantic, and on failure appends a bounded repair turn that quotes the validation errors. It never accepts invalid JSON silently.

**Why it exists.** Local models omit braces, wrap JSON in fences, or add extra keys (`extra="forbid"`). Infinite repair loops waste GPU hours.

**Why this design.** `json.JSONDecoder.raw_decode` finds the first object/array. `max_schema_repair_attempts` (default 2) is additional tries after the first response. `FakeInferenceProvider` lets later agent tests skip HTTP.

**Alternatives considered.** Asking the model for chain-of-thought then parsing (forbidden by spec); unconstrained retries; guided decoding only (not portable across local servers).

**How to replace.** Guided decoding can be added as a provider option later. Keep Pydantic as the acceptance gate.

---

## Phase 5 — Planner

### Planner agent (`src/mobyai/agents/planner.py`)

**What it does.** Builds a Planner prompt from the objective and existing workspace paths, asks the model for `PlannerOutput`, then runs deterministic checks: non-empty DAG, cycles/missing deps, required `acceptance_criteria` and `test_commands`, project-relative paths only. Invalid JSON is handled by `StructuredGenerator`. Invalid DAGs get a bounded repair turn quoting the validator error.

**Why it exists.** The Planner is the architect, not the implementer. The harness must reject giant or cyclic plans before any Worker runs.

**Why this design.** The agent returns a validated `PlannerOutput` and does not touch SQLite or `ProjectState`. That keeps “models propose, application disposes.” Directory scaffolding is empty dirs/parents only — no application source files.

**Alternatives considered.** Letting the model mark tasks `ready` (dependency logic is code); creating stub source files during planning (Worker’s job); regenerating the whole plan on any schema hiccup without DAG-specific repair.

**How to replace.** Swap prompts in `agents/prompts/planner.py`. Add extra atomicity heuristics in `validate_planner_output` without changing the engine loop.

### Planning loop (`src/mobyai/engine/planning.py`)

**What it does.** `plan_project` transitions NEW → PLANNING (persisted before inference), then on success PLAN_READY → READY with tasks, `PLAN_CREATED` checkpoint, and a planner event. On schema/DAG/inference failure: PLANNING → FAILED with `last_error`. Refuses to plan projects that are not NEW or PLANNING (crash resume of an in-flight plan is allowed).

**Why it exists.** Crash during a long planner call must not look like NEW again with a half-written DAG. Persistence happens around the model, not inside it.

**Why this design.** Inspect-then-plan uses `list_tree` so the model sees existing files. The Worker is not called.

**Alternatives considered.** Planning without persisting PLANNING first (loses crash recovery); auto-running the first Worker task (Phase 7).

**How to replace.** Phase 9 added `replan_project` for failed-branch patches. Keep `plan_project` for greenfield objectives; do not reuse it to rewrite a live DAG.

---

## Phase 6 — Context packer

### Token estimate (`src/mobyai/context/tokenizer.py`)

**What it does.** Approximates tokens as `ceil(chars / 4)` and truncates oversize blobs to a head/tail slice.

**Why it exists.** The Worker prompt must stay under `context.worker_token_budget` (default 48000). A real tokenizer would pin the harness to one model family.

**Why this design.** The spec asked for a lightweight counter first. Char/4 is stable, dependency-free, and slightly conservative for most English/code mixes.

**Alternatives considered.** tiktoken / HuggingFace tokenizers (accuracy vs. a hard model coupling); word splits (worse on dense code).

**How to replace.** Swap `estimate_tokens` for a model-specific counter without changing `WorkerContext` or the packer priority rules.

### Import resolver (`src/mobyai/context/imports.py`)

**What it does.** Parses Python `import` / `from` with `ast` and maps one hop of local modules to candidate `.py` / `__init__.py` paths. Stdlib names (`sys.stdlib_module_names`) are skipped.

**Why it exists.** The Worker needs interfaces the target already imports, not the whole repository and not PyPI packages that are not in the workspace.

**Why this design.** One hop from target files only. Missing candidates are dropped; they are not marked missing (unlike declared `target_files`).

**Alternatives considered.** Regex import scraping (fragile); following import graphs transitively (blows the budget); embedding search (out of MVP).

**How to replace.** Add language-specific resolvers behind the same candidate-path list. Do not start walking the full tree.

### ContextPacker (`src/mobyai/context/packer.py`)

**What it does.** `build_worker_context(project_id, task_id, token_budget)` loads project/task/attempts from a `StateStore`, reads files through `WorkspaceFilesystem`, and returns a `WorkerContext`. Files are packed in priority order: target → test → `required_context` → local imports. Over-budget imports are omitted; over-budget targets are truncated. On retry, the latest failed attempt log is reduced with `LogReducer` instead of being dumped in full. `worker_response`, events, and other tasks are never included.

**Why it exists.** The Worker must see a fresh, minimal payload. Conversation history and unrelated files are how long-horizon runs lose the plot.

**Why this design.** The packer is independent of the inference provider. `pack(project, task)` is the core; the spec method is a store-backed wrapper. `WorkerContext.render()` is the tokenized surface so Phase 7 does not reassemble prompts ad hoc.

**Alternatives considered.** Dumping the whole workspace (budget and relevancy both fail); asking the model which files it wants first (extra round-trip, still needs a jail); including full attempt JSON (leaks history the spec forbids).

**How to replace.** Keep `WorkerContext` as the contract. Phase 7 consumes `render()` / `files` rather than rebuilding packing rules inside the Worker agent.

---

## Phase 7 — Worker and safe artifact application

### Worker agent (`src/mobyai/agents/worker.py`)

**What it does.** Builds a prompt from `WorkerContext.render()`, asks the model for `WorkerResult`, and returns structured `tool_calls`. It does not touch the filesystem, run tests, or persist state.

**Why it exists.** The Worker is the implementer, not the source of truth. File bodies in chat must never become writes.

**Why this design.** Same split as the Planner: the agent proposes, `StructuredGenerator` enforces the schema, the engine applies side effects. A bounded extra-message loop lets the model `read_file` then `write_file` without making the agent own the workspace.

**Alternatives considered.** Dumping source into the JSON `summary` and writing it (the spec forbids this); a single-shot with no tool feedback (cannot read then edit); letting `done=true` complete the task (tests are the gate).

**How to replace.** Swap prompts in `agents/prompts/worker.py`. Do not add a second write path that bypasses `ToolDispatcher`.

### Tool dispatcher (`src/mobyai/execution/tools.py`)

**What it does.** Maps `WorkerToolCall` names to `WorkspaceFilesystem`, `apply_patch`, `CommandRunner`, and `TestRunner`. Path escapes, missing arguments, and destructive commands become `ok=false` outcomes; they do not raise through the Worker loop.

**Why it exists.** The application, not the model, must enforce workspace boundaries, argv-only commands, timeouts, and output size.

**Why this design.** One dispatcher so every tool shares the jail. Commands accept argv lists or `shlex.split` strings — never a shell. Worker-requested `run_test` is informational; official pass/fail is still the harness `test_commands` after the loop.

**Alternatives considered.** Giving the model a shell (`/bin/sh -c`); writing files from chat when `tool_calls` is empty; stopping the whole project on a single tool error (report and continue so later calls can still run).

**How to replace.** Add tools by extending `WorkerToolName` and `_dispatch`. Do not parse a free-form command line through a shell.

### Task execution loop (`src/mobyai/engine/working.py`)

**What it does.** `execute_task` transitions READY/RETRYING → DISPATCHING → WORKING (persisted before inference), packs context, runs up to `max_worker_turns`, then WORKING → VALIDATING and runs `test_commands`. Pass requires required files on disk and exit code 0: VALIDATING → READY, task PASSED, dependents refreshed. Fail leaves the project in VALIDATING with the attempt recorded; retry vs escalate is Phase 8. Inference/schema failure is `AttemptResult.ERROR`, not a project FAILED.

**Why it exists.** Crash during a Worker call must not look like READY with no attempt. Completion must not be a model claim.

**Why this design.** Attempt counters increment in application code. `changed_files` on the result come from successful tool outcomes, not `WorkerResult.changed_files`. Chat dumps (`done=true`, empty `tool_calls`) write nothing and fail the missing-file gate.

**Alternatives considered.** Completing on `done=true` (optimistic Worker claims); looping READY→READY on failure inside Phase 7 (that is Orchestrator/retry accounting); failing the whole project on one bad Worker JSON.

**How to replace.** `advance_project` consumes `TaskExecutionResult` and calls `ProjectStateMachine.next_after_validation_failure`. Do not re-apply tool calls there.

---

## Phase 8 — Orchestrator and retry accounting

### Orchestrator agent (`src/mobyai/agents/orchestrator.py`)

**What it does.** Given a harness-required decision (`RETRY_WITH_FIX` or `ESCALATE_TO_PLANNER`), asks the model for `OrchestratorResult` and validates it: the enum must match, `task_id` must be the failed node, retry payloads must target the Worker with an instruction.

**Why it exists.** Interpreting a traceback into a one-line fix is a semantic job. Choosing retry vs escalate is not.

**Why this design.** The prompt states the required decision up front. Wrong enums get a bounded repair turn quoting the validator, same pattern as Planner DAG repair. If the model is down after repairs, the loop still retries/escalates with a fallback instruction from `last_error`.

**Alternatives considered.** Letting the model pick CONTINUE on a failed test (skips retry accounting); asking it which DAG node is next (already `select_next_task`); failing the project when orchestrator JSON is bad (counters still say retry).

**How to replace.** Swap prompts in `agents/prompts/orchestrator.py`. Do not move `next_after_validation_failure` into the prompt.

### Orchestration loop (`src/mobyai/engine/orchestration.py`)

**What it does.** `advance_project` is one outer step: complete if the DAG is done, BLOCKED if nothing is eligible, otherwise dispatch `execute_task`. On failure, `resolve_validation_failure` uses attempt counts: first fail → task READY, VALIDATING → RETRYING, RETRY checkpoint with the remediation instruction; second fail → task FAILED (dependents BLOCKED), VALIDATING → ESCALATING, `EscalationPackage` checkpoint. `run_project` repeats until COMPLETED, FAILED, or BLOCKED. With a `planner`, ESCALATING calls `replan_project` and continues; without one (or after `max_replans`) it returns at ESCALATING.

**Why it exists.** Select / dispatch / evaluate / retry / escalate / complete must be one application-owned cycle. Phase 7 stops at VALIDATING so this layer can own the counters.

**Why this design.** CONTINUE and MISSION_COMPLETE do not call the Orchestrator model. Remediation is injected into `WorkerContext` on the next Worker turn (`## Remediation instruction`). Escalation stores an `EscalationPackage` checkpoint so replan does not rebuild failure context from chat.

**Alternatives considered.** Nested retry inside `execute_task` (hides accounting); completing on Orchestrator `MISSION_COMPLETE` while tasks remain (optimistic); calling the Planner from `resolve_validation_failure` (keeps retry accounting mixed with DAG edits).

**How to replace.** Keep counters in `next_after_validation_failure`. DAG edits belong in `replan_project`.

---

## Phase 9 — Planner escalation / DAG-branch replan

### DAG patch contract (`src/mobyai/models/planner.py`)

**What it does.** `DAGPatch` is the Planner's replan output: `summary`, optional `architecture_summary` and extra `directory_structure`, `supersede[]` (`task_id` plus new `PlannerTaskSpec` replacements), and optional `add_tasks`.

**Why it exists.** After two Worker failures the plan is locally wrong, not globally wrong. A full `PlannerOutput` would discard PASSED work.

**Why this design.** Separate schema from `PlannerOutput` so the model cannot "helpfully" emit a whole new DAG. `supersede` has `min_length=1` because the failed node must be replaced. Replacement specs reuse `PlannerTaskSpec` (same atomicity / tests / relative-path rules as initial planning).

**Alternatives considered.** Regenerating the whole plan (wipes PASSED tasks); editing the failed `Task` in place (collides with attempt history and `task_id` identity); letting the Orchestrator invent replacement nodes (not an architecture job).

**How to replace.** Add optional patch fields with defaults. Do not accept a `PlannerOutput` on the replan path.

### Apply patch (`src/mobyai/dag/patch.py`)

**What it does.** `affected_branch` is the failed node plus transitive successors. `apply_dag_patch` marks superseded nodes `SUPERSEDED` (`superseded_by` = first replacement id), inserts replacements with new ids, retargets remaining dependents onto `replacement_sinks` (replacement nodes that nothing else in that op depends on), then `validate_dag`.

**Why it exists.** The model proposes a patch; the harness must refuse illegal edits before they hit SQLite.

**Why this design.** The patch must supersede the failed task. It cannot supersede PASSED tasks, unknown ids, or nodes outside the failed branch. Replacements cannot reuse a live or superseded id, and cannot depend on a superseded id. FAILED/BLOCKED nodes stay in the table as history; `TaskGraph.is_complete` ignores `SUPERSEDED`.

**Alternatives considered.** Deleting failed rows (loses attempts); attaching dependents to every replacement (over-constrains a linear split); asking the Planner which live edges to rewrite (nondeterministic).

**How to replace.** Keep `apply_dag_patch` as the only writer of SUPERSEDED status. Extra patch ops (split/merge) should go through the same branch and PASSED guards.

### Planner replan (`src/mobyai/agents/planner.py`)

**What it does.** `PlannerAgent.replan` prompts with failed node, reduced errors, attempts, current DAG, and affected branch, then validates `DAGPatch` the same way initial plans are validated (relative paths, required `acceptance_criteria` / `test_commands`, then `apply_dag_patch`). Bounded repair quotes the validator.

**Why it exists.** Interpreting why a branch failed is semantic. Choosing which PASSED tasks to keep is not.

**Why this design.** Dynamic prompt fields are brace-escaped so `{` in tracebacks does not break `.format()`. The agent still does not touch SQLite or `ProjectState`.

**Alternatives considered.** Feeding the full Worker chat (the spec forbids history dumping); skipping DAG-specific repair and failing the project on the first bad patch.

**How to replace.** Swap `REPLAN_SYSTEM_PROMPT` / `REPLAN_USER_PROMPT` / `PATCH_REPAIR_PROMPT`. Do not validate patches only inside the engine.

### Replan loop (`src/mobyai/engine/replanning.py`)

**What it does.** `replan_project` accepts ESCALATING or REPLANNING (crash resume). ESCALATING → REPLANNING is persisted before inference. On success: apply patch, refresh `TaskGraph`, REPLANNING → READY, clear `current_task_id`, optional architecture/scaffold update, `REPLAN` checkpoint, `dag_replanned` event. On schema/DAG/inference failure: REPLANNING → FAILED.

**Why it exists.** Crash during a long Planner call must not look like ESCALATING with a half-applied patch.

**Why this design.** Loads the latest `CheckpointKind.ESCALATION` payload as `EscalationPackage` instead of reconstructing from events. `run_project(..., planner=, max_replans=3)` continues after READY; omitting `planner` preserves the Phase 8 stop-at-ESCALATING behavior.

**Alternatives considered.** Calling `plan_project` again (greenfield path); applying the patch before persisting REPLANNING (crash leaves a live DAG that no checkpoint explains); unbounded replans (oscillating patches).

**How to replace.** Keep `plan_project` for NEW objectives. Do not let the Worker or Orchestrator write `SUPERSEDED`.

---

## Phase 10 — GPU service manager

### Service config (`src/mobyai/models/config.py`)

**What it does.** `services.mode` is `external` or `managed`. Timeouts cover health polling and process stop. `swap_shared_device` (default true) unloads the other occupant of the same configured device. Per-role `launch` is an argv list with `{model}` / `{port}` / `{endpoint}` / `{device}` placeholders.

**Why it exists.** Hardware, ports, and engines must stay in YAML. The application must not hard-code GPU 0/1.

**Why this design.** External is the default so development and unit tests never spawn vLLM. Managed mode is opt-in. A role with empty `launch` stays external even when the global mode is managed (hybrid: orchestrator already running, worker spawned).

**Alternatives considered.** Always spawning (hostile for Cursor/dev without local GPU daemons); shell strings (`shell=True` is forbidden elsewhere); putting launch commands in Python constants.

**How to replace.** Add extra `services` fields with defaults. Keep `extra="forbid"` so unknown keys fail at load.

### GpuServiceManager (`src/mobyai/inference/service_manager.py`)

**What it does.** Implements `InferenceServiceManager`: `ensure_role_available`, `healthcheck`, `stop_role`. External mode healthchecks the configured OpenAI-compatible endpoint and records logical GPU occupancy (`PLANNER_MODE` / `WORKER_MODE` / `IDLE`). Managed mode starts an isolated process with `CUDA_VISIBLE_DEVICES` from `hardware.for_role` (fallback: `models.*.gpu`). CPU/`none` sets an empty CUDA visibility list. Same-device planner/worker swap stops the occupant, then starts the requested role, then waits for health. Spawn uses argv lists and `ProcessSpawner` so tests inject fake processes.

**Why it exists.** Planner and Worker may share a physical GPU over time but must not share a CUDA process with the Orchestrator, and mixed GPU generations must not share a process.

**Why this design.** Lifecycle is a protocol, not vLLM APIs. The engine persists PLANNING / WORKING / REPLANNING first, then calls `ensure_role_available` — persist-then-swap. Callers that omit `services=` keep talking to already-running endpoints. `SubprocessSpawner` is replaceable; unit tests never touch a GPU.

**Alternatives considered.** Loading both large models at once (VRAM); mapping devices inside the serving engine instead of `CUDA_VISIBLE_DEVICES` (breaks isolation); restarting on every `ensure` (slow, unnecessary if already healthy).

**How to replace.** Swap `ProcessSpawner` for a container runner. Keep device numbers in config. Do not import vLLM in `engine/` or `agents/`.

---

## Phase 11 — CLI

### Runtime (`src/mobyai/runtime.py`)

**What it does.** `Runtime` holds `AppConfig`, a `StateStore`, the three agents, and an `InferenceServiceManager`. `Runtime.from_config` is the production wiring (SQLite + OpenAI-compatible clients + `GpuServiceManager`). Tests inject fakes.

**Why it exists.** The CLI must not construct vLLM, SQLite, and agents ad hoc in every command. One object is closable and replaceable.

**Why this design.** Commands call engine functions (`plan_project`, `run_project`, `resume_project`) instead of reimplementing the harness. `owns_store` avoids closing an injected in-memory store.

**Alternatives considered.** A global singleton (hostile to tests); constructing providers inside each subcommand (duplicates timeouts and repair counts).

**How to replace.** Keep `RuntimeFactory` as the injection point. Do not let argparse own persistence.

### Commands (`src/mobyai/cli.py`)

**What it does.** stdlib argparse surface: `init`, `doctor`, `start`, `run`, `status`, `pause`, `resume`, `stop`, `tasks`, `logs`, `models`, `config validate`. `--config` / `MOBYAI_CONFIG` / `./config/default.yaml` / `~/MobyAI/config.yaml`.

**Why it exists.** Operators need a way to create projects, inspect DAG progress, pause, and continue after a crash without a GUI.

**Why this design.** `resume` loads the snapshot first (`resume_project`), unpauses only if `PAUSED`, then continues the same loop as `run`. `doctor` reports config, workspace writes, database, endpoint health, `nvidia-smi`, and launch argv — missing GPU is a warning in external mode and a failure in managed mode. `start` healthchecks all roles when external; managed mode starts only the Orchestrator and leaves Planner/Worker to persist-then-swap during `run`. `stop` pauses the project and `stop_role`s every managed process. Status prints current task, progress, last error, attempt count, configured worker model, and GPU.

**Alternatives considered.** Click/Typer (extra dependency); JSON-only output (worse for a local operator); auto-unpausing inside the store (hides operator intent).

**How to replace.** Add subcommands in `parse_args`. Keep side effects in engine modules. `mobyai doctor` should keep going after a single failed check so the operator sees the full board.

---

## Phase 12 — End-to-end fixture

### Task manager scenario (`tests/fixtures/task_manager/`)

**What it does.** Supplies the objective, generated `src/db.py` / `src/app.py` / `README.md`, and scripted Planner, Worker, and Orchestrator JSON for a three-step DAG: SQLite store, REST-shaped `TaskAPI`, documentation.

**Why it exists.** Phase 12 must exercise the full recovery story without a GPU or a live vLLM process.

**Why this design.** STEP_001 passes. STEP_002 fails twice (empty `tool_calls`) so counters retry then escalate. The Planner returns a `DAGPatch` that supersedes STEP_002 with STEP_002B. STEP_003 is retargeted onto STEP_002B and completes. Harness `test_commands` stay `shlex`-safe file checks; pytest loads the generated modules to assert POST/GET `/tasks` against SQLite.

**Alternatives considered.** Calling real local models (not default-unit-suite); embedding complex `python -c` JSON in `test_commands` (`shlex.split` breaks on nested quotes); regenerating a full plan on escalate (forbidden).

**How to replace.** Swap fixture sources and scripted payloads. Keep the engine loop unchanged. Do not mark the unit suite as requiring GPUs.

### Integration tests (`tests/integration/test_task_manager.py`)

**What they do.** (1) Full lifecycle on `SqliteStateStore`. (2) Close the DB after STEP_001, `resume_project` from the same file, then finish retry/replan/complete. (3) The same scripted agents through `mobyai init` / `mobyai run`.

**Why they exist.** Definition of done includes stop/resume without losing state, and the operator CLI must be able to drive the fixture.

**Why this design.** One `advance_project` for the reconnect cut so `run_project` does not hit `max_steps`. Fake inference queues are reused across the reconnect so remaining Worker/Orchestrator/Planner turns stay aligned.

**Alternatives considered.** In-memory-only e2e (misses crash recovery); pausing via CLI mid-`run_project` (the loop does not yield).

**How to replace.** Add another fixture directory and a new integration module. Do not import vLLM.





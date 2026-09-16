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

That script finds Python 3.12+, creates `.venv`, installs `mobyai`, then runs `mobyai setup`. Press Enter to keep the defaults: Hugging Face token, managed llama.cpp on two GPUs, and the reference Qwen GGUF models. After the token prompt, Enter installs llama.cpp, downloads those GGUF files, and healthchecks `/v1/models`. Type anything else to override a field.

Non-interactive:

```bash
./scripts/setup.sh --yes --output ./config.yaml --force --skip-hf-token --skip-download
```

Equivalent without the script:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
mobyai setup
```

Then `source .venv/bin/activate` in every new shell.

## Local secrets vault

Open the **Secrets** tab to save, replace, or remove tokens for Hugging Face, GitHub,
GitLab, Google Gemini / AI Studio, OpenAI, Anthropic, Mistral, npm, Brave Search,
and Semantic Scholar. These are ten common credential types; saved values are
never returned to the browser. Removal requires the confirmation checkbox.

Install the optional integration, then restart the console:

```bash
cd ~/moby-ai-assistent
.venv/bin/python -m pip install -e '.[vault]'
```

On Fedora, the local open-source vault is GNOME Keyring (Secret Service).
Use **Passwords and Keys** to create/unlock your default login keyring. Keep a
non-empty keyring password for encrypted storage at rest. If needed, install the
desktop components with `sudo dnf install gnome-keyring seahorse`. Run MobyAI as
your desktop user in the same D-Bus session, not with sudo. There is no plaintext
fallback; a locked or unavailable vault displays instructions in the tab.

Hugging Face downloads and Brave/Semantic Scholar research use saved tokens when
existing environment credentials (and the legacy Hugging Face token file) are
absent. Other providers are currently **storage-only**: saving a key does not
configure an inference provider, authenticate Git, or expose it to worker shell
commands. The Google entry accepts a Gemini API key, not a Workspace password or
Google Scholar credential. Tokens are not added to prompts, trace attributes,
project files, YAML, or MobyAI's SQLite database. Existing token files are not
migrated or deleted automatically.

The Secrets API is limited to the loopback, same-origin console and requires a
per-console anti-CSRF token for changes. The desktop vault is protected at rest,
but is not an isolation boundary against processes running as the same user in
an unlocked session. Do not run untrusted generated code under an account that
has access to sensitive credentials.

## Starting after installation

Use [`scripts/start.sh`](scripts/start.sh) to launch the installed application:

```bash
./scripts/start.sh
```

This activates the repository virtual environment, loads `~/MobyAI/env.sh` if
present, and opens the operator console at `http://127.0.0.1:8743` by default.
To restart an already-running console, use this from any directory:

```bash
~/moby-ai-assistent/scripts/restart.sh
```

The restart helper verifies the process belongs to this checkout and configuration,
stops the idle console gracefully, then starts the updated UI in your terminal.
If a workload is active, stop it using Monitor's two confirmations first. Wait for
any directory copy to finish. It also works when the console is not running.
Optional flags: `--config /path/to/config.yaml`, `--port 8743`, and `--no-browser`.

To shut down without restarting, run this from another terminal (any directory):

```bash
~/moby-ai-assistent/scripts/shutdown.sh
```

It verifies the console belongs to this checkout/config, requests a clean pause
for its active workload, waits for any directory copy, then closes the console
through its cleanup path and waits for exit. Project files and saved progress are
preserved; later use Start and **Resume** to continue. Cleanup closes owned model
processes and flushes enabled tracing. Separately launched CLI jobs and external
model servers are left running. The script does not force-kill the console;
workload stopping uses the existing 45-second grace period and recorded fallback.

Optional flags: `--config /path/to/config.yaml`, `--port 8743`, and
`--timeout 180` (increase this for long directory copies). If nothing is running,
it exits successfully. No sudo is needed.

It does not reinstall packages. Keep the terminal open; Ctrl+C stops the console.
Use **New workload** in the console to import a prompt and explicitly start coding work.

To run an existing project, open another terminal:

```bash
./scripts/start.sh run demo
./scripts/start.sh status demo
```

Managed mode loads inference servers as needed during `run`. No separate
`mobyai start` command is required. A project already marked `FAILED` must be
recovered separately; this launcher does not reset project state.

The launcher uses the repository `config.yaml`, or `MOBYAI_CONFIG` when set.
Override it before the command:

```bash
./scripts/start.sh --config /path/to/config.yaml ui --no-browser
```

Run `./scripts/start.sh --help` for usage. The script also works when called
by its absolute path from another directory.

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
- Local llama.cpp (`llama serve`) after `./scripts/setup.sh`, **or** your own OpenAI-compatible servers
- Optional Hugging Face token (`HF_TOKEN` or `~/MobyAI/secrets/hf_token`) for gated Hub repos
- Optional NVIDIA GPUs for managed-process mode; unit and integration tests do not require GPUs

## Configuration

`mobyai setup` writes a commented `config.yaml` with reference Hugging Face models. Press Enter to keep them, or pass `--planner-model` / `--orchestrator-model` / `--worker-model`. The Hub token is stored in `~/MobyAI/secrets/hf_token`, not in YAML.

`--config PATH` overrides the search order: `MOBYAI_CONFIG`, `./config.yaml`, `./config/default.yaml`, `~/MobyAI/config.yaml`.

Validate with:

```bash
mobyai --config ./config.yaml config validate
mobyai --config ./config.yaml models
```

## Starting inference servers

External mode (`mobyai setup --preset external`) expects Planner, Orchestrator, and Worker OpenAI-compatible servers already listening on the configured ports. The **setup default** is managed llama.cpp.

```bash
mobyai --config ./config.yaml doctor
mobyai --config ./config.yaml start
```

`start` healthchecks `/v1/models` first. If a role is already serving, it is reused. Otherwise managed mode spawns `llama serve`.

Managed mode (`config/examples/managed.yaml`, or `mobyai setup --mode managed`) lets MobyAI spawn isolated llama.cpp processes with `CUDA_VISIBLE_DEVICES` from `hardware`. Planner and Worker that share a device are swapped; they are not loaded at the same time.

## Import a Nautilius prompt

After initial setup, run `~/moby-ai-assistent/scripts/start.sh` and open
`http://127.0.0.1:8743`. If the console is already running, stop it with Ctrl+C
and restart it to load the updated UI.

1. In [Nautilius Prompt Bench](https://github.com/mettetsystems/nautilius-prompt-bench), export a rendered `optimized_prompt.txt` or `canonical_prompt.md` (the other TXT/MD variants also work).
2. Open **New workload**, enter a project name, and choose the file or paste its contents.
3. Review the full prompt and click **Create and start**. Monitor opens for the new project and refreshes its launch output, events, and task progress.

Imports accept UTF-8 TXT/MD up to 1 MB; structured JSON/YAML specifications must
first be rendered by Nautilius. The complete text is saved as the project objective
without truncation. The model's configured context window still limits what it can
process. Each import gets a fresh workspace beneath your configured workspace root, or in the optional target directory you enter.

Keep the console process running: stopping it also interrupts its workload. Only
one console-launched workload runs at a time. Launch logs are saved under
`logs/workloads` beside the database. To resume an interrupted project, use
`./scripts/start.sh run <project_id>` with the ID shown in Monitor.

## Operator console

```bash
mobyai --config ./config.yaml ui
```

Opens a local page (default `http://127.0.0.1:8743`) with **New workload** (prompt import and launch), **Settings** (temperature, top-p, top-k, model ids, endpoints) and **Monitor** (collapsible DAG steps, last 100 log lines, failed steps expanded with the full event/attempt workflow). Apply writes `config.overlay.yaml` beside the main YAML.

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

## Recovery, stopping, and MLflow tracing

Monitor's **Stop workload** button works for workloads launched by that console.
Confirm once, then type the exact project ID in the second verification dialog.
The server checks an expiring, single-use confirmation tied to the running process.
Stopping cancels the active inference/tool call, terminates command subprocesses,
and saves a **PAUSED** project. Interrupted tasks rerun from their saved workspace;
completed tasks remain complete. Partial file edits are retained, not rolled back.
If graceful shutdown exceeds 45 seconds, the console records `stop_forced` and kills
the workload process group. The button remains unavailable for external CLI runs.
Resume with the separate **Resume** button or `./scripts/start.sh run <project_id>`.

Errors now include categories and repair instructions. For example, npm's
`Missing script: "test:bpm"` requires checking `package.json`, the working directory,
and actual tests; renaming the task or adding a no-op script is not a repair.
Network, permission, system-toolchain, and capacity failures pause for operator intervention.
Recognized missing Node development tools (such as Jest) instead enter local dependency
repair without sampling escalation. Agents are guided to inspect the manifest and
lockfile, install declared development dependencies, and use equivalent package test
scripts. Saved bare test commands can resolve installed `node_modules/.bin` tools
in the working directory or its ancestors within the workspace; no automatic
download or global installation is performed by this lookup.
Transient inference transport errors and HTTP 429/502/503/504 receive at most two
retries with bounded backoff and unchanged sampling; authentication/bad-request
responses are not retried. Tool outcomes appear in Monitor as they complete.

After a replan, subsequent failed execution attempts advance Planner/Worker through
these request-local recovery profiles; a successful task resets the stage:

The harness uses a bounded cyclic coordinate schedule: no extra model calls are
spent searching for sampling settings. This is a recovery heuristic, not a proven
optimizer: code and prompts change between attempts, so test outcomes do not
isolate the effect of sampling. Model-specific targets are operator configurable.

Each eligible failed run advances **one setting at a time**, in this order:
**temperature → top-k → top-p**. Other values carry forward unchanged. Values
already in effect are skipped. A successful task resets to the configured baseline.
For the default baseline (`temperature=0`, `top_k=null`, `top_p=1`), the first steps are:

| Stage | Temperature | Top-k | Top-p |
| --- | --- | --- | --- |
| Baseline | 0 | null | 1 |
| 1 | 0.1 | null | 1 |
| 2 | 0.1 | 5 | 1 |
| 3 | 0.1 | 5 | 0.70 |
| 4 | 0.2 | 5 | 0.70 |
| 5 | 0.2 | 8 | 0.70 |
| 6 | 0.2 | 8 | 0.75 |

The same sequence continues through six target presets, up to 18 individual
changes, ending at temperature 0.6, top-k 20, top-p 0.95. These are configurable
presets, **not universal coding maxima**, and a change need not increase a value.
Replace `models.planner.recovery_sampling` and `models.worker.recovery_sampling`
with up to six `{temperature, top_p, top_k}` target mappings, or `[]` to disable
sampling escalation. Custom targets also expand into single-setting changes from
the role's live baseline. Operator edits to that baseline rebuild the ladder.
Existing saved stage numbers use the new expanded sequence after upgrading.
Individual attempts retain their tool-turn and repair limits; project recovery
continues across attempts. Network retries keep the same sampling settings.
The orchestration model provides remediation advice; the harness enforces the
profiles and per-attempt limits, and leaves the orchestrator's sampling unchanged. Schema
and semantic-plan repairs keep the first repair at baseline, then use the recovery
profiles within their existing repair limits. Recovery never bypasses validation.
`runtime.max_replans` is now a compatibility field only. Exhaustive recovery has
no project replan-count cap; history is retained for diagnosis.

Install and enable [MLflow tracing](https://mlflow.org/docs/latest/genai/tracing/app-instrumentation/manual-tracing/):

```bash
.venv/bin/python -m pip install -e '.[tracing]'
MOBYAI_MLFLOW_ENABLED=1 ./scripts/start.sh
```

Alternatively add `tracing: {enabled: true}` to your config or local overlay.
The default destination is `mlflow.db` beside the MobyAI database, with experiment
`MobyAI`. `tracing.tracking_uri` or `MLFLOW_TRACKING_URI` selects another backend;
`tracing.experiment` changes the experiment name. Only enable a remote destination
you intend to receive the trace metadata.

```bash
./scripts/tracing.sh
```

The console's **Traces** tab is the lightweight default: it reads 25 trace summaries
at a time and loads operations only when you select a trace. Filter by status,
refresh manually, and expand operations to inspect timing, sampling, tokens, and
error types. It makes no background polling requests and starts no extra server.
Large traces display at most 1,000 operations. Traces may not appear until the root
workload span closes; use **Monitor** for live activity.

The optional command above opens the full MLflow viewer at `http://127.0.0.1:5000`.
It is not required for the Traces tab. Workload spans contain planner/replanner,
orchestrator, worker attempts, tool/command execution, schema repairs, and HTTP
attempts. Model spans record effective sampling and token counts. Prompts, source
contents, raw command arguments, and exception messages are not exported; local
workload/test logs and Monitor retain the diagnostic details. Trace export failures
warn without replacing the application's original error.

[LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) provides durable
execution, checkpoints, and human-in-the-loop interrupts. A migration may help if
this engine expands into parallel/distributed workflows, but it does not repair
invalid npm scripts or model-generated code. This implementation strengthens the
existing engine without adding a second orchestration runtime.


## Restart, resume, and target directories

Monitor provides separate controls:

- **Resume** continues a stopped, incomplete project in its saved workspace. It keeps the plan, completed tasks, attempt history, and recovery progress. Completed and terminally failed projects use Restart instead.
- **Restart** asks for confirmation, then creates a new project ID from the same name and original prompt, with a fresh plan and budget. Previous files and history remain intact. Specify a new target directory, or leave it blank to use a fresh directory under the configured workspace root.
- **Change target** copies a stopped project's workspace to the entered directory in the background. Monitor shows copy status. After copying succeeds, the saved workspace path changes; the original directory remains available. Resume then uses the new path.

The **New workload** tab also accepts an optional target directory. Paths refer to
the machine running MobyAI, not the browser's machine. Use an absolute path or
`~/...`; the destination must be new or empty. Existing files and other registered
project workspaces are never intentionally merged or overwritten. Source and
destination cannot contain one another. Stop active work before changing targets
or restarting. Execution locks prevent overlapping runs of the same project from
updated CLI/console processes.

Directory copying retains symlinks and the existing workspace access restrictions.
Absolute paths embedded in generated code or scripts may need adjustment after
relocation. If copying fails, inspect Monitor's current target and event history;
the destination may hold a partial copy. Keep the console open until copying ends.

The Monitor tab displays total runtime across runs and resumes (excluding pauses), plus elapsed time including pauses. Timing is saved in project events and refreshes with Monitor auto-refresh. A fresh restart has its own timer; older runs without timing events show no saved history, and interrupted sessions without a finish event are marked estimated.

Workload execution (`run` and `resume`, including jobs launched by Monitor) requests
a temporary systemd idle/sleep inhibitor by default. It releases on completion,
pause, error, or process exit; an idle console does not keep the PC awake. The
launch log reports whether protection was acquired. Missing tools or denied
permissions produce a warning and allow execution without protection. Set
`runtime.keep_awake: false` to opt out. This does not change permanent power
settings or disable the screen lock, and cannot prevent forced shutdowns or power
loss. Restart the console to load this behavior for newly launched workloads.

Agent prompts use role-specific guidance: the Planner decomposes requirements
into verifiable outcomes without a minimum step count; the replanner preserves
acceptance criteria and valid work while repairing the failed branch; the Worker
rereads stale patch context and verifies results; the Orchestrator gives evidence-
based remediation within harness-owned decisions. These prompts guide behavior;
they do not add automatic review passes or increase per-attempt tool limits. Restart
the idle console to load them for new workload processes. Prompt changes alone
are not evidence of improved model performance; compare workload completion,
validation failures, repeated repairs, and runtime on representative projects.

### Targeted research through local MCP

Install the optional server/client dependency:

```bash
.venv/bin/python -m pip install -e '.[research]'
```

The planner and worker can now call `research_search` through a fixed local stdio
MCP server launched on demand. No listening port or separate background service is
required. The orchestrator can recommend research to those agents; it does not
execute tools itself. For another MCP host, configure the absolute path to
`scripts/research.sh` as its stdio command.

Example tool arguments:

```json
{"query":"Gradle 8.4 wrapper project directory site:docs.gradle.org","error":"Gradle build directory not found","source":"web"}
```

| Source | Access |
| --- | --- |
| `zenodo` | Public records API; live retrieval verified during integration |
| `semantic_scholar` | Academic Graph API; optional `SEMANTIC_SCHOLAR_API_KEY`; anonymous access can be rate-limited |
| `dblp` | Public search API; challenges are reported, never bypassed |
| `web` | Brave Search API with `BRAVE_SEARCH_API_KEY`; otherwise a manual search link |
| `google_scholar`, `papers_with_code`, `citeseerx`, `baidu_scholar`, `scielo` | Site-scoped Brave web-index search with that key; otherwise manual links. This is not direct access to each site's internal search index. Papers with Code service availability may vary. |

Export keys in the environment used to launch the console (or in the existing
local `~/MobyAI/env.sh`), then restart the idle console. Do not put keys in prompts
or commit them. Google Workspace does not supply a Google Scholar API key; this
integration does not use a Google API key. Google Custom Search JSON API is closed
to new customers and existing users must migrate by January 1, 2027.

Research is methodical: inspect local evidence first, search a public error
signature plus technology/version, read at most five hits, choose one hypothesis,
and validate it locally before broadening. Academic sources are intended for
algorithmic questions; prefer official product documentation for build failures.
Queries are limited to 240 characters with basic path/credential redaction; this
is not a guarantee of secret detection. Never send full logs or proprietary code.

The shared cache (`~/.cache/mobyai/research.sqlite3`, override with
`MOBYAI_RESEARCH_CACHE`) uses the vector-cache retention policy below. Requests are spaced at least
ten seconds apart across MCP processes, with 60 requests/hour and three uncached
queries per normalized error signature per 24 hours. Limits apply across projects
sharing the cache. There are no automatic network retries; 429 responses preserve
`Retry-After`, and access challenges impose a one-hour cooldown. Requests return
explicit unavailable/manual-link statuses instead of invented evidence. Results
contain URLs, timestamps, and metadata snippets, not verified full text. Worker
research results also appear in Monitor events. Local models still validate fixes
with the harness; research evidence does not bypass validation.

References: [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x),
[DBLP API](https://dblp.org/faq/How%2Bto%2Buse%2Bthe%2Bdblp%2Bsearch%2BAPI.html),
[Semantic Scholar API](https://api.semanticscholar.org/api-docs),
[Zenodo API](https://developers.zenodo.org/),
[Brave Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get),
[Google search API changes](https://programmablesearchengine.googleblog.com/2026/01/updates-to-our-web-search-products.html).

### Local research vector cache and retention

Research now checks an exact cache and then a CPU-only sparse text-vector index
in the same SQLite database before contacting a provider. No GPU, embedding-model
download, additional server, or cloud embedding API is needed. This is lexical
cosine similarity, **not neural semantic embeddings**: it handles overlapping or
reordered wording, but does not reliably recognize synonyms. Searches remain
source-specific and must match version numbers, site restrictions, negations,
and recognized error codes. Similar matches carry the original query, requested
query, similarity score, and cache age so the agent can assess relevance.

Defaults:

- Fresh positive results: 14 days; empty results: one day.
- Delete entries unused for four weeks, even if historically popular.
- Delete entries fetched more than 12 weeks ago regardless of usage. A fresh API
  retrieval renews that age; cache reads do not renew evidence freshness.
- Cap at 2,000 entries; evict least recently used entries first, with hit counts
  breaking ties. Weekly exact/similar hit totals are retained for 12 weeks.
- Retention cleanup runs on cache access at most once daily and after new writes.
  No background scheduler is required. SQLite reuses freed pages; logical pruning
  does not necessarily shrink the database file immediately.

Optional environment settings, passed through the local MCP bridge:
`MOBYAI_RESEARCH_CACHE_FRESH_DAYS`, `MOBYAI_RESEARCH_CACHE_IDLE_WEEKS`,
`MOBYAI_RESEARCH_CACHE_MAX_AGE_WEEKS`, and `MOBYAI_RESEARCH_CACHE_MAX_ENTRIES`.
Use positive integers and restart the console after changing its environment.
The `force_refresh` search argument requests current evidence, but still respects
all API budgets and cooldowns. Existing cache entries are indexed locally without
network requests. Purging evidence never clears provider budgets or cooldowns.

Inspect utilization:

```bash
~/moby-ai-assistent/.venv/bin/python -m mobyai.research.cache stats
```

Preview a purge (no evidence deleted), then apply it:

```bash
~/moby-ai-assistent/.venv/bin/python -m mobyai.research.cache purge --idle-weeks 4 --max-age-weeks 12
~/moby-ai-assistent/.venv/bin/python -m mobyai.research.cache purge --idle-weeks 4 --max-age-weeks 12 --apply
```

The purge flags override policy for that invocation. Use `--path` for a different
cache. Only cached research metadata and snippets are pruned, not project files,
workload checkpoints, or model weights.

### Role-aware, cache-first recovery

BM25 is a lexical ranking algorithm, not an embedding model. Local
`research_lookup` now combines BM25 and normalized text-vector cosine ranks with
reciprocal-rank fusion, entirely on CPU. It retrieves candidates from cached
queries, titles, and snippets; relevance is not proof that a proposed fix works.
The exact/similar-query cache still avoids redundant API requests independently.

For eligible implementation failures with the default two-attempt budget:

1. First failure: ordinary worker repair, no outbound research.
2. Second failure: the orchestration harness calls local `research_lookup` via
   MCP and gives the results and logs to the Orchestrator model.
3. The Orchestrator explicitly sets `cached_references_sufficient`. If true and
   candidates exist, the harness permits one additional cache-assisted worker
   attempt, supplying references and the unchanged validation commands. The
   sampling stage is held during this recovery phase.
4. The Worker reports `reference_urls_used`; the harness records supplied versus
   self-reported used references separately. A failed reference-assisted attempt
   is reported to the Orchestrator and cannot repeatedly renew this extra attempt.
5. Missing/insufficient evidence or failed cached repair produces a
   `[RESEARCH_REQUIRED]` Planner handoff. Only this tagged replan, after at least
   two attempts, enables the planner's `research_search` tool. The planner must
   review supplied logs/attempts before searching. Provider limits still apply.

Workers and initial planning may call `research_lookup`, which never contacts
external APIs. The Worker cannot call the outbound research tool. The Orchestrator
uses MCP through its harness-owned lookup step and judges applicability; it does
not run arbitrary tools. The standalone MCP server remains usable by other MCP
hosts; role gating is enforced by MobyAI's dispatcher and replan context.
Infrastructure failures retain their existing pause behavior. Project recovery
has no replan-count cap; the exhaustive recovery policy below applies.
Reference-use reports are model declarations; only harness validation establishes
whether the attempted repair passed. Restart the idle console to load this flow.

### Exhaustive project recovery

Normal `run`/`resume` execution has **no replan-count or orchestration-step cap**.
The legacy `runtime.max_replans` field is accepted for compatibility but ignored;
increasing it is no longer necessary. Saved replan history remains available.
Previously budget-paused projects clear that obsolete error when resumed.
Explicit `--max-steps N` is an operator-requested clean pause, not a budget error.

Worker tool-turn and schema-repair limits still bound each individual attempt,
then feed failure evidence into another repair/replan round. Failed planner
validation starts another planning round with error feedback and a short backoff
(up to 30 seconds), rather than ending the project because its repair count ran
out. Cache-assisted recovery, references, and research provider pacing remain in
place. Research API quotas are separate and do not stop the project by themselves.

Context-capacity exhaustion remains a reason to pause and preserve progress.
Unrecoverable infrastructure/access problems and operator Stop can still pause
execution; a software bug cannot be solved by repeatedly calling an unavailable
service. This policy does not guarantee convergence or make individual model
contexts unlimited. Use Monitor and Stop to control an unproductive run.

### Delete an old project

Monitor now has **Delete project** for inactive projects. Confirm the action and
then type the exact project ID. MobyAI deletes the project's database records
(tasks, attempts, checkpoints, contracts, and events) atomically. It keeps the
workspace directory, log files, research cache, and MLflow traces. Deletion cannot
be undone in the UI. Stop an active workload cleanly before deleting it; active
runs and directory-copy operations are protected by execution locks. No existing
projects are deleted when installing this feature.

### Review and improve an existing codebase

Greenfield creation remains the default. In **New workload**, choose **Review and
improve an existing codebase**, enter its absolute directory and your improvement
prompt, then select **Review codebase and propose plan**.

- **MobyAI-native:** the Logic Reviewer reads recorded plans, supplied model
  reasoning and validation outcomes. Its reasoning logic report informs the new
  plan alongside your improvement prompt.
- **Other codebases:** a read-only Code Reviewer performs code archaeology using
  a local AST graph, BM25 search and graph-neighbor expansion. Its functionality
  report informs the new plan. Application code is not executed during archaeology.
- **Both workflows:** Monitor displays the proposed tasks, validation commands and
  `review_influence` notes. Check the review acknowledgment and select **Approve
  plan and start** to permit implementation. Resume alone cannot bypass approval.
  Source changes before approval/first execution invalidate the reviewed snapshot.
  Leave the plan unapproved to keep it paused; submit a fresh review with a revised
  prompt if you want a different proposal.

The Logic Reviewer and Code Reviewer are separate prompt/response roles using the
existing Planner model and its managed service lifecycle. They do not allocate a
second Qwen instance or execute Worker tools. Existing run/resume/restart/stop,
recovery, tracing and deletion workflows remain available.

### mobyMinutes and reports

New planning runs create **`mobyMinutes/`** at the codebase root. Each run has its
own directory containing the original prompt, full accepted plans and subsequent
DAG patches, raw model requests/responses (including model-returned
`reasoning_content` when present), local MLflow span snapshots when tracing is
enabled, and task validation outcomes. Failed planning attempts are recorded too.
Missing reasoning is explicitly unknown; generated rationale summaries are not
represented as recovered internal reasoning. Old projects cannot retroactively
recover traces that were never recorded.

The **Generate logic report** button in Monitor creates JSON and readable Markdown
reports. During a workload, the request is queued until a safe step boundary so
review and Worker inference do not compete for the GPU. Completion also attempts
a logic report automatically. Report failures appear in Monitor and can be retried;
they do not turn a successfully completed project into a failed project.

Top-level report files:

- `mobyMinutes/reasoning-logic-report.md` and `.json`
- `mobyMinutes/functionality-report.md` and `.json` for imported non-native code
- `mobyMinutes/runs/<run-key>/` retains per-run reports, proposals, plan records,
  and the reusable `code-graph.sqlite3` for archaeology runs.

The new payload archive is local to the workspace; existing global MLflow tracing
continues to export metadata rather than full prompts. These local minutes contain
prompts and source excerpts: include them deliberately when sharing a codebase.
Deleting a project from Monitor continues to preserve all workspace files,
including its minutes. Older archived task outcomes remain usable after database
project deletion; a missing historical outcome is reported as unknown.

To generate a report without resuming coding:

```bash
./scripts/start.sh report PROJECT_ID
```

### Local archaeology tooling and coverage

Install the optional parsers on another host with:

```bash
.venv/bin/python -m pip install -e '.[archaeology]'
```

Python uses its standard-library AST. Tree-sitter adapters cover JavaScript/JSX,
TypeScript/TSX, Go, Rust, Java, C and C++. Several other common source formats are
indexed as text with explicit coverage warnings. Parsers are installed locally;
indexing neither downloads grammars nor sends code to a search API. Review inference
uses the configured Planner endpoint, so use a local endpoint to keep the entire
workflow on-host.

The graph stores file/symbol nodes, containment, lexical neighbors, resolvable
imports and *candidate* call links. SQLite FTS5/BM25 selects seeds; retrieval adds
one-hop incoming and outgoing neighbors. This is static AST-backed GraphRAG,
not a complete dynamic call graph. The reviewer can request up to four targeted
queries per turn, for up to five turns, and records uncertainty and partial coverage.
The index skips dependencies, generated directories, symlinks and likely secret
files; files above 512 KB are not indexed. Indexing is bounded to 10,000 eligible
files. Reports use bounded context and stop with an explicit review error when
evidence exceeds it, rather than silently authorizing a partial plan.

References: [Tree-sitter Python API](https://github.com/tree-sitter/py-tree-sitter),
[MLflow manual tracing](https://www.mlflow.org/docs/latest/genai/tracing/app-instrumentation/manual-tracing/).

Restart the **idle** console after upgrading to load the new backend routes. No
existing projects need to be deleted or set up again.


## Running coding jobs over SSH

Run these commands **on the MobyAI host**, after connecting through SSH. Prompt
files and target directories refer to that host. Use absolute paths with the
startup wrapper, which changes into the repository directory.

```bash
# Create a project without starting it. The prompt is a UTF-8 text file (max 1 MiB).
~/moby-ai-assistent/scripts/start.sh init --name my-job --id my-job \
  --prompt-file /home/ojoseph/prompts/job.txt \
  --target-directory /home/ojoseph/MobyAI/projects/my-job

# Run with top-level progress; -vv adds recent current-step tool/error events.
~/moby-ai-assistent/scripts/start.sh run my-job -v

# In a second SSH terminal: view once, or follow until the execution stops.
~/moby-ai-assistent/scripts/start.sh status
~/moby-ai-assistent/scripts/start.sh monitor my-job -vv --follow
~/moby-ai-assistent/scripts/start.sh pause my-job
~/moby-ai-assistent/scripts/start.sh resume my-job -vv

# Delete saved project records, preserving the target directory and files.
~/moby-ai-assistent/scripts/start.sh delete my-job --confirm my-job
```

`--workspace` remains an alias for `--target-directory`. This sets the workspace
at project creation; it does not relocate an existing project's files. Plain
`run` retains its existing concise output. `-v` shows state, progress, and current
step; `-vv` includes recent current-step events and tool results. These flags work
on `run`, `resume`, and `monitor`. Ctrl+C in a separate monitor exits only that
monitor. The monitor reads saved events, not live token streams or hidden model
reasoning.

Use a persistent terminal session such as `tmux` for jobs that must survive an SSH
disconnection: start `tmux new -s moby`, run the command, and detach with Ctrl+B,
then D. Reconnect with `tmux attach -t moby`. Jobs otherwise remain foreground
processes; the CLI does not silently create a background service.

`pause` requests cancellation from an active runner, which cleans up its current
operation and saves a resumable state. Wait for `PAUSED` before resuming. A task
interrupted mid-operation is retried using its existing workspace. Already-running
processes must be restarted to load this pause support. No model fine-tuning
backend is included; these commands run coding jobs.

### Tokens over SSH

```bash
# Hidden terminal prompt: the value is not a command-line argument.
~/moby-ai-assistent/scripts/start.sh secrets set huggingface
# Alternatively read a protected UTF-8 token file, or use --file - for stdin.
~/moby-ai-assistent/scripts/start.sh secrets set brave --file /secure/path/brave-token
~/moby-ai-assistent/scripts/start.sh secrets list
~/moby-ai-assistent/scripts/start.sh secrets remove brave --confirm brave
```

Secrets use the same local Secret Service vault as the UI. The SSH session must
have access to your user's session D-Bus and an unlocked keyring. If the session
bus address is missing but your user bus is running, it can be selected with
`export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"`.
A locked or unavailable vault produces an error; there is no plaintext fallback.
The commands accept single API tokens, not JSON credentials or SSH private keys,
and never offer a command to display saved values. `secrets list` explains which
providers are integrated and which are storage-only; storing a token does not
automatically grant agents access to it. Existing environment/file credentials
retain their precedence.

# Local MCP recovery tools and log review

## Observed failures

Read-only review on 2026-10-01 of `~/MobyAI/mobyai.db` and its adjacent
`mobyai.db.records` artifacts. The stored events cover 2026-09-28 01:36 UTC through
2026-10-01 05:08 UTC: 3 project runs, 63 attempts, 974 events and 4,956 capture
entries. Counts below use the 496 authoritative `tool_finished` events, not the
duplicate capture entries. A failed tool call can recover within its attempt;
these counts are not counts of project stops or distinct underlying bugs.

| Tool | Failed / called | Observed pattern | Added capability |
| --- | ---: | --- | --- |
| `apply_patch` | 77 / 84 | 48 context mismatches; 25 patches with no file hunks; 2 hunks without headers; 2 invalid diff arguments | `edit_file` replaces one exact text span after inspection, with an optional SHA-256 guard |
| `run_test` | 42 / 47 | TypeScript/Jest errors, genuine assertions, missing scripts, wrong runner or no discovered tests | `inspect_node_project` exposes scripts and dependency evidence before tests; actual test results remain authoritative |
| `run_command` | 26 / 43 | 23 npm dependency conflicts; one blocked `rm`, one invalid package name, one exit 1 with no captured error text | `inspect_node_project` exposes installed versions and peer requirements; installation still uses the existing command policy |
| `read_file` | 5 / 204 | Missing paths | `file_info` distinguishes files, directories, missing entries and symlinks |
| `list_directory` | 1 / 31 | Missing directory | `file_info` and `create_directory` |
| `write_file` | 0 / 58 | No recorded failures | Existing tool retained |
| `search_code` / `glob` | 0 / 29 | No recorded failures | Existing tools retained |

The most common captured command was `npm ci --include=dev` (20 calls), followed
by the project-specific npm test scripts. Repeated installation cannot resolve
incompatible peer requirements by itself; the new tools provide evidence for
choosing compatible declared versions, not permission to bypass the resolver.
`npm ci` also cannot update a mismatched lockfile and removes an existing
`node_modules` before installation, so blind repetition can discard working
dependencies without resolving the conflict. See [npm's command reference](https://docs.npmjs.com/cli/v11/commands/npm-ci/).

There were 11 schema-failed attempts. The 48 recorded schema-rejection events
included 29 arrays instead of result objects, 12 bare tool/patch responses, one
per-call-summary response, and six other shape errors. No unknown-tool enum
rejection was found in these artifacts. MCP does not replace WorkerResult
validation; schema guidance, bounded repair and narrow metadata normalization
still apply.

At the snapshot, Splash-Prompt-Builder was paused with the per-call-summary error
while checking venv existence and package versions. Both FlowBPM runs were paused
at the 1,800-second inference timeout approval limit. Additional tools do not lift
that limit or resume projects automatically.

Two attempts also reported `Missing required files: .venv/`. The harness used
`is_file()` even for explicitly directory-shaped requirements. It now checks
`is_dir()` for paths ending in `/`, while file requirements still require files.

## Available servers

The dispatcher launches two fixed local MCP servers using the current Python
interpreter and stdio. They have no listening network port. The MCP Python SDK
is a core dependency, pinned to the v1 compatibility line (`mcp>=1.30,<2`), matching
the existing research/sandbox integrations and [official v1 SDK guidance](https://py.sdk.modelcontextprotocol.io/v1/).

| Server | Tools | Access |
| --- | --- | --- |
| `mobyai-workspace` | `file_info`, `create_directory`, `edit_file` | Worker gets all three; planner gets `file_info` |
| `mobyai-environment` | `inspect_python_environment`, `inspect_node_project` | Worker and planner; read-only |

The Worker calls these names directly in its usual `tool_calls` array; the harness
routes them through MCP. Schemas come from the same registry for prompt guidance,
local validation and MCP discovery. No settings changes or model-selected server
commands are required. Existing `read_file`, `write_file`, `apply_patch`,
`run_command` and `run_test` retain their execution paths. Research and sandbox
MCP servers retain their existing access rules.

`edit_file` requires `path`, `old_text` and `new_text`. Read the file first and pass
its `source_sha256` as optional `expected_sha256`. Missing, ambiguous or stale
matches fail without writing. Updates use atomic replacement, preserve line
endings and executable permissions, and report the changed path. Patch failures
recommend this alternative instead of repeating a stale or headerless diff.

`inspect_python_environment` accepts a venv `path` (default `.venv`) and optional
`packages` containing distribution names. It reads `pyvenv.cfg`, interpreter entry
metadata and installed distribution versions. It does not execute interpreter
symlinks or import project code. `inspect_node_project` accepts a project `path`
(default `.`), and reads scripts, lockfile presence, declared dependencies,
installed versions and peer requirements. Neither proves runtime availability,
dependency compatibility, sandbox readiness or passing tests. Results identify
these limits; files and inventories have explicit size/count bounds.

## Runtime and evidence

Each invocation owns a bounded MCP session and closes its child on completion,
timeout or cancellation. The deadline is at most 60 seconds and no greater than
the dispatcher's configured tool timeout. No call is automatically replayed after
a transport failure; the next attempt must inspect workspace state first. The
child receives only a small environment allowlist plus harness-selected roots.
Project source and installed package metadata are never executed by these servers.

Workspace and operator-configured additional roots are enforced in the child.
Read-only sessions cannot discover or invoke mutation tools. Planners cannot call
`edit_file` or `create_directory`. Existing command sandboxing, approved dependency
artifacts, test gates and timeout ceilings remain in force. No generic shell,
arbitrary MCP endpoint, dependency-force flag or deletion tool is added.

Run capture includes `local_mcp_started`, `local_mcp_finished` and cancellation
events in the same step as the enclosing tool request/result evidence.

## Prompt and memory mitigations

Worker and planner prompts require fresh file inspection before edits and exact
peer-version evidence before retrying an ERESOLVE failure. They discourage repeated
installation when the required local tools already work. Dependency conflicts are
classified as locally repairable, with sampling escalation disabled; the harness
still owns retries, replanning and validation.

After a mutation attempt (including failed patches and commands), the worker
rebuilds its file context and invalidates older successful inspection bodies in
conversation history. Historical hashes remain labelled historical. This works
even when optional full per-turn refresh is off. Optional history compaction now
also removes old successful `edit_file` payloads and local MCP inspection bodies,
while preserving failures and recent turns. Reads preserve original line endings
so `read_file` hashes and exact edits agree on CRLF files.

The harness records compact `tool_recovery_memory` events immediately after each
patch/edit result or relevant npm-install result, before later calls can change
the evidence. The record contains an operation fingerprint, bounded error,
affected-file hashes and next-check guidance. Npm snapshots include manifests,
lockfiles, `.npmrc` and npm's installed-tree lockfile; file contents and credentials
are not copied into these snapshots.

Recall considers the latest 100 such events, keeps at most three matching scopes,
and uses at most 600 tokens within the existing worker memory budget. It deduplicates
repeated failures, survives task renames, ignores file states that changed, and
clears a scope after success. Edit memories must overlap the task's files. The
orchestrator/replanner also receive these observations in recovery evidence.
This is advisory evidence, not a permanent prohibition on another attempt: facts
outside the fingerprint, such as registry or runtime changes, still need checking.
Historical runs are not retroactively assigned guessed fingerprints.

For an independent local MCP client, launch:

```bash
MOBYAI_LOCAL_WORKSPACE=/absolute/project/path python -m mobyai.local_tools.server workspace
MOBYAI_LOCAL_WORKSPACE=/absolute/project/path python -m mobyai.local_tools.server environment
```

These commands speak MCP over stdio; do not type ordinary shell input into them.
Set `MOBYAI_LOCAL_READ_ONLY=1` for a read-only workspace server. Moby supplies these
values itself. Restart an already-running Moby process to load the new schemas
and dispatcher before resuming a saved project.

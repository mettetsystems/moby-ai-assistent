# Durable coding-run records

Moby records observable coding activity in three linked research records. These
records provide evidence for future evaluation and learning research. They do not
extract lessons, create playbooks, change model weights, or modify agent behavior.

## The three records

| Record | Lifetime | Contents |
| --- | --- | --- |
| `RunRecord` | One project lifecycle | Original objective, baseline state, lifecycle transitions, execution boundaries, final project result, and links to its plan and attempts |
| `StepRunRecord` | One task attempt | Task specification, effective initial model request, all captured turns and model calls, tool and command activity, remediation context, validation, and attempt outcome |
| `PlanRecord` | One project lifecycle | Planning and replanning requests, proposals, rejected output and validation errors, applied plans, human edits and approvals, and final-pass additions |

Records have UUID IDs, schema version `1`, UTC creation/update timestamps, and a
capture-completeness field. Each history entry has its own UUID, UTC timestamp,
project-wide sequence number, actor, and payload reference. Parent revision links
associate changes and worker attempts with the active applied plan revision.
Sequence numbers establish order even when timestamps match.

A `plan_validated` entry means the proposal passed structural validation.
`plan_applied` means it was installed in the harness's task graph. Human approval
is recorded separately. An applied plan is not a claim that its code changes have
already been implemented.

A `StepRunRecord` outcome is `in_progress`, `pass`, `fail`, `error`, `timeout`, or
`interrupted`. The harness's test/validation results determine success, not the
worker's statement that it is done. If the harness resumes the same unfinished
attempt, recording reuses its ID and starts a new execution segment. If the harness
starts another attempt instead, that attempt receives a new StepRunRecord.

## Capture at execution boundaries

Capture is enabled automatically. It also operates through the direct engine
entry points, independently of the CLI and MLflow configuration.

The model capture hook records the effective request after prompt selection,
response-schema augmentation, and live sampling settings. It records provider
model identity, responses, token usage when returned, schema repairs, and model
errors. It retains model-returned content; it does not reconstruct missing internal
reasoning. Existing Settings prompt overrides remain authoritative, and later
prompt edits do not rewrite earlier artifacts.

For the known worker format drift of string `summary` fields inside individual
tool calls, a `schema_normalized` entry records the original response and exact
removed field paths. The full normalized response must pass strict validation
before execution. Other schema failures retain the existing `schema_rejected`
and bounded model-repair records.

Tool and command records separate the requested action from its observed result.
Their start and finish entries share a correlation UUID. Tool arguments include
submitted write and patch payloads. Command results include available stdout,
stderr, exit status, timeout, and truncation information. A worker context entry
preserves the task briefing and remediation instruction. Harness validation has
its own boundaries and result payload.

Local workspace/environment MCP calls also record `local_mcp_started`,
`local_mcp_finished`, or `local_mcp_cancelled` entries with a shared invocation
ID, server group and tool name. These supplement the enclosing tool request and
outcome. A transport failure is recorded without automatically replaying a write.

These are observations, not replay instructions. An unmatched start means that no
completion was recorded. Moby does not infer success or replay actions because of
research-record data. Arbitrary changes made inside shell commands are represented
by the command and its observed results, not an invented file-by-file patch.
Existing output limits still apply; truncated command/tool outputs are identified.

## Storage and artifacts

SQLite migration 4 adds `record_runs`, `record_steps`, `record_entries`, and
`record_artifacts` to the existing state database. The record repository commits
incrementally. SQLite is the canonical source; ordinary JSON bundles are exports.

Large payloads are JSON artifacts stored adjacent to the configured database. For
`/path/mobyai.db`, the artifact directory is `/path/mobyai.db.records/`. Files are
addressed by SHA-256 and reused when identical. Their references contain a portable
relative path, SHA-256, byte count, media type, and truncation flag. Record history
contains these references rather than repeatedly embedding large prompts or logs.
The in-memory test store keeps artifacts in process memory and is not durable.

This evidence is additional local disk usage. It requires no additional inference
model, vector database, embedding service, or message broker. Writes await a local
durable capture boundary; background vector indexing is outside this phase.
Credential configuration, authentication headers, and environment dumps are not
collected. Captured prompts, source edits, and command output are nevertheless
project data and can themselves contain sensitive text. Capture is not a general
secret-scrubbing system. Artifact and export files are written with owner-only file
permissions.

## Exporting portable JSON

Use a destination that does not already exist:

```bash
mobyai records export PROJECT_ID --output /path/to/new-export
```

The command does not run models or execute project tools. Its bundle contains:

```text
new-export/
  manifest.json
  run.json
  plan.json
  steps/
    <step-record-uuid>.json
  artifacts/
    <hash-prefix>/<sha256>
  schemas/
    RunRecord.schema.json
    StepRunRecord.schema.json
    PlanRecord.schema.json
```

The export takes a consistent database snapshot, validates available artifact
hashes, and copies the required payloads. The manifest lists file sizes and hashes,
export time, schema version, completeness, and missing/corrupt artifacts. The three
JSON Schema documents allow downstream tools to validate the exported records.
Artifact paths are relative to the bundle root.

Exports never overwrite an existing directory. Missing or corrupted evidence
produces a usable but explicitly partial bundle. A filesystem error while writing
an export can leave an unfinished directory; use a new destination for a retry.
Only a bundle with a completed `manifest.json` should be treated as finished.

## Failure handling and history limits

Research-capture failures are best effort: they emit a warning and do not turn a
passing coding task into a failure. Pending warnings are persisted once recording
recovers, and exports include warnings still held in the running process. Existing
execution-state persistence errors retain their previous behavior.

Snapshots compare captured steps and project state with authoritative execution
state. Missing step records, missing completion evidence, and unmatched boundaries
are exposed rather than reconstructed into fictional history. Completeness means:

- `complete`: no detected capture gaps for the record's captured interval.
- `partial`: known missing evidence, existing uncaptured history, or capture errors.
- `unknown`: boundaries have no observed completion; work may still be active or
  may have been interrupted.

No recorder can preserve a failure diagnostic on storage that remains unavailable
until process termination. Recovery detects discrepancies where evidence exists;
`complete` is not a proof against every possible storage or process failure.

Existing projects capture forward when execution next begins. Their baseline
includes available task/attempt/checkpoint information and the legacy `mobyMinutes`
location. Their old activity is not backfilled into new steps or fabricated
prompts. Capture is labeled partial. Existing `mobyMinutes`, reports, and MLflow
traces continue to operate.

Deleting saved project state also removes its canonical research records.
Unreferenced artifacts are removed after state deletion commits; content shared
with another project remains. Previously exported bundles are independent copies
and remain untouched. There is no automatic retention or experience-promotion
policy in this phase.

## Future learning boundary

The current records are episodic evidence: what was requested, attempted, observed,
and validated. A future experience library must distinguish that evidence from
semantic lessons and procedural strategies. Neither a successful run nor repeated
model assertions automatically become a rule.

Stable record IDs, sequence numbers, revision links, and content hashes provide a
foundation for later asynchronous archival or indexing. Memory scopes, semantic
compression, Milvus collections, temporal conflict handling, decay, and validated
experience promotion require separate designs and are not implemented here.

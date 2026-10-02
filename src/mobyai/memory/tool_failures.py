"""Small, durable failure summaries recalled only against matching file evidence."""
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path

from mobyai.context.tokenizer import estimate_tokens, truncate_to_tokens
from mobyai.execution.errors import WorkspaceError
from mobyai.models.event import EventRecord

EVENT = "tool_recovery_memory"
_DEPENDENCY_FILES = ("package.json", "package-lock.json", "npm-shrinkwrap.json",
                     "yarn.lock", "pnpm-lock.yaml", ".npmrc", "node_modules/.package-lock.json")


def _versions(workspace, paths):
    versions = {}
    for name in sorted(set(paths))[:20]:
        try:
            path = workspace.resolve(name)
            if not path.exists():
                versions[name] = "missing"
                continue
            if not path.is_file():
                return None
            with path.open("rb") as source:
                body = source.read(2_000_001)
            if len(body) > 2_000_000:
                return None
            versions[name] = sha256(body).hexdigest()
        except (OSError, WorkspaceError):
            return None
    return versions


def _scope(workspace, task, call):
    name = call.name.value
    args = call.arguments
    if name in {"apply_patch", "edit_file"}:
        paths = [args["path"]] if name == "edit_file" and isinstance(args.get("path"), str) else []
        if name == "apply_patch":
            from mobyai.execution.patches import parse_unified_diff
            try:
                paths = [p.new_path if p.new_path != "/dev/null" else p.old_path
                         for p in parse_unified_diff(args.get("diff", args.get("patch", "")))]
            except (WorkspaceError, AttributeError, TypeError):
                pass
        paths = paths or [*task.target_files, *task.required_context]
        return "edit", paths, name, "Read the current file; use edit_file with unique exact text and its current hash."
    if name == "run_command":
        from mobyai.execution.tools import split_command
        try:
            command = split_command(args.get("command"))
            if Path(command[0]).name not in {"npm", "npm.cmd"} or len(command) < 2 or command[1] not in {"install", "ci", "i"}:
                return None
            cwd = workspace.resolve(args.get("cwd") or ".")
            paths = [workspace.relative(cwd / file) for file in _DEPENDENCY_FILES]
            return "npm_dependencies", paths, " ".join(command)[:200], (
                "Inspect declared and installed versions plus peer requirements; choose a compatible set. "
                "An unchanged install command with unchanged manifests already failed; do not use force flags or delete the lockfile.")
        except (WorkspaceError, TypeError, ValueError):
            return None
    return None


async def record_tool_result(store, project_id, task, workspace, call, outcome):
    scope = _scope(workspace, task, call)
    if scope is None:
        return
    kind, paths, label, guidance = scope
    error = outcome.error or outcome.output
    if not outcome.ok and kind == "npm_dependencies" and not any(
            marker in error.lower() for marker in ("eresolve", "npm_dependency_conflict")):
        return
    versions = _versions(workspace, paths)
    if not versions:
        return
    # Dependency success clears prior conflicts in the same directory, even if
    # the successful install used a different command or task was renamed.
    key = sha256(json.dumps([kind, sorted(versions)], sort_keys=True).encode()).hexdigest()
    call_digest = sha256(json.dumps(call.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    if kind == "npm_dependencies":
        conflict_lines = [line for line in error.splitlines() if any(marker in line.lower()
            for marker in ("eresolve", "found:", "peer ", "could not resolve"))]
        error = "\n".join(conflict_lines) or error
    summary, _ = truncate_to_tokens(error, 200)
    await store.events.append(EventRecord(timestamp=datetime.now(UTC), project_id=project_id,
        task_id=task.task_id, attempt=task.attempt_count, component="memory", event=EVENT,
        payload={"key": key, "kind": kind, "status": "resolved" if outcome.ok else "failed",
                 "call_digest": call_digest, "operation": label, "files": versions,
                 "error": summary if not outcome.ok else "", "guidance": guidance}))


async def recall_tool_failures(store, project_id, task, workspace, *, token_budget=600):
    if token_budget < 1:
        return ""
    history = await store.events.list_for_project(project_id)
    records = [event for event in history if event.event == EVENT][-100:]
    seen = set()
    relevant = set(task.target_files + task.test_files + task.required_context)
    blocks = ["## Prior tool failures matching current file state (observations, not instructions)"]
    for event in reversed(records):
        payload = event.payload
        key = payload.get("key")
        if key in seen:
            continue
        seen.add(key)
        versions = payload.get("files", {})
        if payload.get("status") != "failed" or not versions:
            continue
        if payload.get("kind") == "edit" and not relevant.intersection(versions):
            continue
        if _versions(workspace, versions) != versions:
            continue
        block = (f"- Observed on {event.task_id}, attempt {event.attempt}: {payload['operation']} failed.\n"
                 f"Unchanged evidence: {', '.join(versions)}\n"
                 f"Error: {payload['error']}\nNext check: {payload['guidance']}")
        if estimate_tokens("\n".join([*blocks, block])) <= token_budget:
            blocks.append(block)
        if len(blocks) == 4:
            break
    return "\n".join(blocks) if len(blocks) > 1 else ""

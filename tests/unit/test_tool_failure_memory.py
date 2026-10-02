import json

import pytest

from mobyai.context.history import compact_history, invalidate_stale_reads
from mobyai.context.packer import ContextPacker
from mobyai.context.tokenizer import estimate_tokens
from mobyai.engine.working import execute_task
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.diagnostics import diagnose
from mobyai.execution.tools import ToolOutcome, ToolDispatcher
from mobyai.execution.workspace import Workspace
from mobyai.memory.tool_failures import record_tool_result, recall_tool_failures
from mobyai.models.inference import ChatMessage
from mobyai.models.worker import WorkerToolCall
from mobyai.state.memory import InMemoryStateStore
from mobyai.state.sqlite import SqliteStateStore
from tests.helpers import make_project, make_task
from tests.unit.test_worker import _agent, _ready_store, _write_app_payload


def npm_call():
    return WorkerToolCall(name="run_command", arguments={"command": ["npm", "ci", "--include=dev"]})


def conflict():
    return ToolOutcome(name="run_command", ok=False, output=(
        "failure_category: npm_dependency_conflict\nnpm error code ERESOLVE\n"
        "npm error Found: react@18.2.0\nnpm error peer react@^19.0.0 from renderer@19.0.0"))


def test_dependency_conflicts_request_local_repair_without_sampling_changes():
    diagnosis = diagnose("npm error code ERESOLVE unable to resolve dependency tree")
    assert diagnosis.category == "npm_dependency_conflict"
    assert diagnosis.locally_repairable
    assert not diagnosis.sampling_eligible
    assert "inspect_node_project" in diagnosis.instruction


async def test_dependency_failure_survives_rename_but_not_changed_manifest(tmp_path):
    store = InMemoryStateStore()
    workspace = Workspace(tmp_path)
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"18.2.0"}}')
    task = make_task("STEP_001", target_files=["app.ts"])
    await record_tool_result(store, "proj-1", task, workspace, npm_call(), conflict())
    renamed = task.model_copy(update={"task_id": "STEP_001_NEW_PLAN"})
    recalled = await recall_tool_failures(store, "proj-1", renamed, workspace)
    assert "react@18.2.0" in recalled and "^19.0.0" in recalled
    assert "STEP_001, attempt" in recalled
    assert estimate_tokens(recalled) <= 600
    assert not await recall_tool_failures(store, "another-project", renamed, workspace)
    assert not await recall_tool_failures(store, "proj-1", renamed, workspace, token_budget=10)
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"19.0.0"}}')
    assert not await recall_tool_failures(store, "proj-1", renamed, workspace)


@pytest.mark.parametrize("changed", ["package-lock.json", ".npmrc", "node_modules/.package-lock.json"])
async def test_dependency_state_changes_invalidate_memory(tmp_path, changed):
    workspace = Workspace(tmp_path)
    store = InMemoryStateStore()
    task = make_task("STEP_001")
    (tmp_path / "package.json").write_text("{}")
    await record_tool_result(store, "proj-1", task, workspace, npm_call(), conflict())
    path = tmp_path / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new state")
    assert not await recall_tool_failures(store, "proj-1", task, workspace)


async def test_success_clears_conflict_even_for_different_install_command(tmp_path):
    workspace = Workspace(tmp_path)
    store = InMemoryStateStore()
    task = make_task("STEP_001")
    await record_tool_result(store, "proj-1", task, workspace, npm_call(), conflict())
    success_call = WorkerToolCall(name="run_command", arguments={"command": ["npm", "install", "--include=dev"]})
    await record_tool_result(store, "proj-1", task, workspace, success_call, ToolOutcome(name="run_command", ok=True))
    assert not await recall_tool_failures(store, "proj-1", task, workspace)


async def test_memory_persists_in_sqlite_and_enters_next_worker_context(tmp_path):
    database = tmp_path / "state.db"
    workspace = Workspace(tmp_path)
    task = make_task("STEP_001", target_files=["app.ts"])
    project = make_project(workspace_root=tmp_path)
    async with SqliteStateStore(database) as store:
        await store.projects.save(project)
        await store.tasks.save(project.project_id, task)
        await record_tool_result(store, project.project_id, task, workspace, npm_call(), conflict())
    async with SqliteStateStore(database) as store:
        packer = ContextPacker(WorkspaceFilesystem(workspace), store, memory_token_budget=1200)
        context = await packer.build_worker_context(project.project_id, task.task_id, 4000)
        assert "Prior tool failures" in context.shared_memory
        assert "react@18.2.0" in context.shared_memory
        assert estimate_tokens(context.shared_memory) <= 1200


async def test_patch_memory_uses_current_source_and_ignores_unrelated_task(tmp_path):
    workspace = Workspace(tmp_path)
    store = InMemoryStateStore()
    task = make_task("STEP_001", target_files=["app.py"], required_context=[])
    (tmp_path / "app.py").write_text("new source\n")
    call = WorkerToolCall(name="apply_patch", arguments={"diff": "-stale\n+replacement"})
    await record_tool_result(store, "proj-1", task, workspace, call,
                             ToolOutcome(name="apply_patch", ok=False, error="Patch contained no file hunks"))
    assert "edit_file" in await recall_tool_failures(store, "proj-1", task, workspace)
    unrelated = task.model_copy(update={"target_files": ["other.py"]})
    assert not await recall_tool_failures(store, "proj-1", unrelated, workspace)
    (tmp_path / "app.py").write_text("changed again\n")
    assert not await recall_tool_failures(store, "proj-1", task, workspace)


def test_invalidated_read_keeps_failure_evidence_and_compacts_old_edit_payload():
    edit = {"summary": "edited", "tool_calls": [{"name": "edit_file", "arguments": {
        "path": "app.py", "old_text": "obsolete source", "new_text": "also old", "expected_sha256": "hash"}}]}
    outcomes = [{"name": "read_file", "ok": True, "output": "obsolete source", "source_sha256": "hash"},
                {"name": "apply_patch", "ok": False, "error": "observed mismatch"}]
    messages = [ChatMessage(role="assistant", content=json.dumps(edit)),
                ChatMessage(role="user", content="Results:\n" + json.dumps(outcomes, indent=2))]
    invalidated = invalidate_stale_reads(messages)
    assert "obsolete source" not in invalidated[1].content
    assert "observed mismatch" in invalidated[1].content
    assert "hash" in invalidated[1].content
    successful = [ChatMessage(role="assistant", content=json.dumps(edit)),
                  ChatMessage(role="user", content="Results:\n" + json.dumps([{"name": "edit_file", "ok": True}], indent=2))]
    compacted = compact_history(successful, retain_pairs=0)
    assert "obsolete source" not in compacted[0].content
    assert "Do not replay" in compacted[0].content
    assert "obsolete source" in messages[1].content


async def test_mutation_refreshes_context_without_optional_refresh_setting(tmp_path):
    store, workspace = await _ready_store(tmp_path)
    (workspace.root / "src").mkdir()
    (workspace.root / "src/app.py").write_text("old_unique_source\n")
    write = _write_app_payload("new_unique_source\n")
    write["done"] = False
    provider, agent = _agent([
        json.dumps({"summary": "Read", "tool_calls": [{"name": "read_file", "arguments": {"path": "src/app.py"}}]}),
        json.dumps(write), json.dumps({"summary": "Done", "done": True}),
    ])
    assert not agent.refresh_context_enabled
    result = await execute_task(await store.projects.get("proj-1"), "STEP_001", store, agent, workspace)
    assert result.passed
    final_request = provider.requests[-1]
    assert "new_unique_source" in final_request.messages[1].content
    assert "old_unique_source" not in "\n".join(message.content for message in final_request.messages)


async def test_read_file_hash_can_be_used_for_crlf_mcp_edit(tmp_path):
    (tmp_path / "app.py").write_bytes(b"first\r\nsecond\r\n")
    dispatcher = ToolDispatcher.from_workspace(Workspace(tmp_path))
    read = await dispatcher.execute(WorkerToolCall(name="read_file", arguments={"path": "app.py"}))
    edit = await dispatcher.execute(WorkerToolCall(name="edit_file", arguments={
        "path": "app.py", "old_text": read.output, "new_text": read.output.replace("second", "changed"),
        "expected_sha256": read.source_sha256,
    }))
    assert edit.ok, edit.error
    assert (tmp_path / "app.py").read_bytes() == b"first\r\nchanged\r\n"

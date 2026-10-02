import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from mobyai.engine.working import _missing_required_files, execute_task
from mobyai.execution.errors import WorkspaceError
from mobyai.execution.tools import PLANNER_INSPECT_TOOLS, ToolDispatcher
from mobyai.execution.workspace import Workspace
from mobyai.local_tools.client import call_local_tool, session_for
from mobyai.local_tools.contracts import TOOLS, READ_ONLY_TOOLS
from mobyai.local_tools.operations import (
    edit_file, file_info, inspect_node_project, inspect_python_environment,
)
from mobyai.models.planner import PlannerInspectCall
from mobyai.models.worker import WorkerToolCall
from tests.helpers import make_task
from tests.unit.test_worker import _agent, _ready_store


def test_exact_edit_preserves_line_endings_permissions_and_hash(tmp_path):
    target = tmp_path / "run.py"
    before = b"#!/usr/bin/python\r\nvalue = 1\r\n"
    target.write_bytes(before)
    target.chmod(0o755)
    result = edit_file(Workspace(tmp_path), "run.py", "value = 1", "value = 2", sha256(before).hexdigest())
    assert target.read_bytes() == before.replace(b"1", b"2")
    assert target.stat().st_mode & 0o777 == 0o755
    assert result["sha256"] == sha256(target.read_bytes()).hexdigest()
    assert result["changed"]
    assert not list(tmp_path.glob(".*.moby-*"))


@pytest.mark.parametrize("old,expected", [("absent", None), ("same", None), ("prefix", "0" * 64)])
def test_rejected_edit_never_changes_file(tmp_path, old, expected):
    target = tmp_path / "file.txt"
    target.write_text("prefix same same\n")
    with pytest.raises(WorkspaceError):
        edit_file(Workspace(tmp_path), "file.txt", old, "changed", expected)
    assert target.read_text() == "prefix same same\n"


def test_file_info_missing_root_directory_and_external_symlink(tmp_path):
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    assert file_info(workspace, ".")["kind"] == "directory"
    assert file_info(workspace, "missing")["exists"] is False
    external = tmp_path / "secret"
    external.write_text("never returned")
    (workspace.root / "link").symlink_to(external)
    info = file_info(workspace, "link")
    assert info["kind"] == "symlink"
    assert info["target_within_workspace"] is False
    assert info["target_exists"] is None
    assert "sha256" not in info
    with pytest.raises(WorkspaceError):
        edit_file(workspace, "link", "never", "changed")
    with pytest.raises(WorkspaceError):
        file_info(workspace, "../secret")
    assert external.read_text() == "never returned"


def test_python_inspection_reads_distribution_metadata_without_executing(tmp_path):
    assert inspect_python_environment(Workspace(tmp_path))["exists"] is False
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.14.7\nhome = /usr/bin\n")
    (venv / "bin/python").symlink_to(sys.executable)
    dist = venv / "lib/python3.14/site-packages/Pydantic-2.13.4.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Name: pydantic\nVersion: 2.13.4\n\nUnused long description")
    result = inspect_python_environment(Workspace(tmp_path), packages=["Pydantic", "missing"])
    assert result["configuration"]["version"] == "3.14.7"
    assert result["installed"]["pydantic"]["version"] == "2.13.4"
    assert result["not_found"] == ["missing"]
    assert result["interpreters"][0]["target_within_workspace"] is False
    assert "unverified" in result["evidence"]


def test_node_inspection_exposes_peer_evidence_and_never_runs_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "app", "packageManager": "npm@10",
        "scripts": {"test": "touch should-not-exist"},
        "dependencies": {"react": "18.2.0"}, "devDependencies": {"react-test-renderer": "19.0.0"}}))
    (tmp_path / "package-lock.json").write_text("{}")
    installed = tmp_path / "node_modules/react-test-renderer"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text(json.dumps({"version": "19.0.0", "peerDependencies": {"react": "^19.0.0"}}))
    result = inspect_node_project(Workspace(tmp_path))
    assert result["scripts"]["test"] == "touch should-not-exist"
    assert result["lockfiles"] == ["package-lock.json"]
    assert result["installed"]["react-test-renderer"]["peer_dependencies"] == {"react": "^19.0.0"}
    assert result["installed"]["react"]["version"] is None
    assert not (tmp_path / "should-not-exist").exists()


async def test_real_stdio_discovery_matches_registry_and_readonly_is_enforced(tmp_path):
    workspace = Workspace(tmp_path)
    async with session_for("workspace", workspace, read_only=True) as session:
        available = await session.list_tools()
        assert [tool.name for tool in available.tools] == ["file_info"]
        response = await session.call_tool("create_directory", {"path": "forbidden"})
        assert response.isError
        assert not (tmp_path / "forbidden").exists()
    for group in ("workspace", "environment"):
        async with session_for(group, workspace) as session:
            available = await session.list_tools()
            assert {tool.name for tool in available.tools} == {name for name, spec in TOOLS.items() if spec.group == group}
            for tool in available.tools:
                assert tool.inputSchema == TOOLS[tool.name].arguments.model_json_schema()
    with pytest.raises(ValidationError):
        PlannerInspectCall(name="edit_file", arguments={})
    for name in READ_ONLY_TOOLS:
        assert PlannerInspectCall(name=name).name.value == name
        assert name in PLANNER_INSPECT_TOOLS


async def test_real_stdio_tools_execute_and_report_changed_files(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "file.txt").write_text("old\n")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"pytest"}}')
    dispatcher = ToolDispatcher.from_workspace(workspace)
    calls = [
        WorkerToolCall(name="file_info", arguments={"path": "file.txt"}),
        WorkerToolCall(name="create_directory", arguments={"path": "new/subdir"}),
        WorkerToolCall(name="edit_file", arguments={"path": "file.txt", "old_text": "old", "new_text": "new"}),
        WorkerToolCall(name="inspect_python_environment", arguments={}),
        WorkerToolCall(name="inspect_node_project", arguments={}),
    ]
    outcomes = await dispatcher.execute_all(calls)
    assert all(outcome.ok for outcome in outcomes), outcomes
    assert outcomes[2].changed_files == ["file.txt"]
    assert (tmp_path / "file.txt").read_text() == "new\n"
    assert (tmp_path / "new/subdir").is_dir()
    assert json.loads(outcomes[3].output)["exists"] is False
    rejected = await dispatcher.execute(calls[2], allowed=PLANNER_INSPECT_TOOLS)
    assert not rejected.ok
    malformed = await dispatcher.execute(WorkerToolCall(name="edit_file",
        arguments={"path": "file.txt", "old_text": "new", "new_text": "bad", "replace_all": True}))
    assert not malformed.ok
    assert (tmp_path / "file.txt").read_text() == "new\n"


async def test_failed_patch_recommends_mcp_edit(tmp_path):
    dispatcher = ToolDispatcher.from_workspace(Workspace(tmp_path))
    result = await dispatcher.execute(WorkerToolCall(name="apply_patch", arguments={"diff": "-old\n+new"}))
    assert not result.ok
    assert "edit_file" in result.error
    assert "source_sha256" in result.error


async def test_transport_failure_is_reported_without_replay(tmp_path, monkeypatch):
    calls = []
    @asynccontextmanager
    async def broken(*args, **kwargs):
        calls.append(args)
        raise OSError("transport down")
        yield
    monkeypatch.setattr("mobyai.local_tools.client.session_for", broken)
    result = await call_local_tool("create_directory", {"path": "output"}, Workspace(tmp_path))
    assert not result.ok
    assert "not replayed" in result.error
    assert len(calls) == 1
    assert not (tmp_path / "output").exists()


async def test_cancellation_closes_session_and_propagates(tmp_path, monkeypatch):
    entered = asyncio.Event()
    closed = []
    class Pending:
        async def call_tool(self, *args):
            entered.set()
            await asyncio.Event().wait()
    @asynccontextmanager
    async def session(*args, **kwargs):
        try:
            yield Pending()
        finally:
            closed.append(True)
    monkeypatch.setattr("mobyai.local_tools.client.session_for", session)
    task = asyncio.create_task(call_local_tool("file_info", {"path": "."}, Workspace(tmp_path)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


def test_directory_requirements_are_not_checked_as_files(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / "file.py").write_text("pass\n")
    task = make_task("STEP_001", target_files=[".venv/", "file.py", "missing/"], test_files=[])
    assert _missing_required_files(Workspace(tmp_path), task) == ["missing/"]
    task = task.model_copy(update={"target_files": ["file.py/", ".venv"]})
    assert _missing_required_files(Workspace(tmp_path), task) == ["file.py/", ".venv"]


async def test_worker_uses_mcp_edit_and_records_evidence(tmp_path):
    command = f'{sys.executable} -c ' + repr("from pathlib import Path; assert Path('src/app.py').read_text() == 'new\\n'")
    store, workspace = await _ready_store(tmp_path, test_commands=[command])
    (workspace.root / "src").mkdir()
    (workspace.root / "src/app.py").write_text("old\n")
    provider, agent = _agent([json.dumps({"summary": "Apply inspected change", "done": False,
        "tool_calls": [{"name": "edit_file", "arguments": {"path": "src/app.py", "old_text": "old", "new_text": "new"}}]}),
        json.dumps({"summary": "Edit applied; ready for harness validation", "done": True})])
    result = await execute_task(await store.projects.get("proj-1"), "STEP_001", store, agent, workspace)
    assert result.passed
    assert result.changed_files == ["src/app.py"]
    assert "inspect_node_project" in provider.requests[0].messages[0].content
    bundle = await store.records.snapshot("proj-1")
    kinds = [entry.kind for step in bundle.steps for entry in step.history]
    assert kinds.count("local_mcp_started") == kinds.count("local_mcp_finished") == 1

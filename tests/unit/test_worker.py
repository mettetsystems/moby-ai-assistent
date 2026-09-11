import json
import sys
from pathlib import Path

import pytest

from mobyai.agents.worker import WorkerAgent
from mobyai.engine.working import WorkingError, execute_task
from mobyai.execution.workspace import Workspace
from mobyai.inference.fake import FakeInferenceProvider, FakeInferenceServiceManager
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.attempt import AttemptResult
from mobyai.models.checkpoint import CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import FROZEN_NOW, make_project, make_task

PYTHON = sys.executable


def _passing_test_command(relative_path: str = "src/app.py") -> str:
    code = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({relative_path!r}).is_file() else 1)"
    )
    return f"{PYTHON} -c {code!r}"


def _failing_test_command() -> str:
    return f"{PYTHON} -c {'raise SystemExit(1)'!r}"


def _write_app_payload(contents: str = "def run():\n    return 1\n") -> dict[str, object]:
    return {
        "summary": "Wrote src/app.py",
        "done": True,
        "changed_files": ["src/app.py"],
        "tool_calls": [
            {
                "name": "write_file",
                "arguments": {"path": "src/app.py", "contents": contents},
            }
        ],
    }


def _agent(responses: list[str], *, json_repairs: int = 2) -> tuple[FakeInferenceProvider, WorkerAgent]:
    provider = FakeInferenceProvider(responses)
    generator = StructuredGenerator(provider, max_repair_attempts=json_repairs)
    return provider, WorkerAgent(generator)


async def _ready_store(
    tmp_path: Path,
    *,
    test_commands: list[str] | None = None,
) -> tuple[InMemoryStateStore, Workspace]:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root, state=ProjectState.READY)
    await store.projects.save(project)
    task = make_task(
        "STEP_001",
        status=TaskStatus.READY,
        target_files=["src/app.py"],
        test_files=[],
        acceptance_criteria=["src/app.py exists"],
        test_commands=test_commands or [_passing_test_command()],
    )
    await store.tasks.save(project.project_id, task)
    return store, workspace


async def test_execute_task_applies_tools_and_passes(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    dependent = make_task(
        "STEP_002",
        status=TaskStatus.PENDING,
        dependencies=["STEP_001"],
        target_files=["src/other.py"],
        test_commands=[_passing_test_command("src/other.py")],
        acceptance_criteria=["other exists"],
    )
    await store.tasks.save("proj-1", dependent)
    _provider, agent = _agent([json.dumps(_write_app_payload())])

    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )

    assert result.passed is True
    assert result.project.state == ProjectState.READY
    assert result.task.status == TaskStatus.PASSED
    assert result.attempt.result == AttemptResult.PASS
    assert result.changed_files == ["src/app.py"]
    assert (workspace.root / "src" / "app.py").read_text(encoding="utf-8") == (
        "def run():\n    return 1\n"
    )
    snapshot = await store.load_snapshot("proj-1")
    assert snapshot is not None
    assert snapshot.tasks[0].status == TaskStatus.PASSED
    assert snapshot.tasks[1].status == TaskStatus.READY
    kinds = [item.kind for item in snapshot.checkpoints]
    assert CheckpointKind.TASK_DISPATCH in kinds
    assert CheckpointKind.TASK_COMPLETION in kinds
    events = await store.events.list_for_project("proj-1")
    assert [item.event for item in events] == ["task_dispatched", "task_passed"]
    contract = await store.contracts.get("proj-1", "src/app.py")
    assert contract is not None
    assert contract.task_id == "STEP_001"
    assert "def run()" in contract.rendered


async def test_chat_dump_does_not_write_files(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    payload = {
        "summary": "def run():\n    return 1\n",
        "done": True,
        "changed_files": ["src/app.py"],
        "tool_calls": [],
    }
    _provider, agent = _agent([json.dumps(payload)])
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert result.passed is False
    assert result.project.state == ProjectState.VALIDATING
    assert result.task.status == TaskStatus.TESTING
    assert result.attempt.result == AttemptResult.FAIL
    assert "src/app.py" in result.missing_files
    assert not (workspace.root / "src" / "app.py").exists()
    stored = await store.projects.get("proj-1")
    assert stored is not None
    assert stored.last_error is not None
    assert "src/app.py" in stored.last_error
    assert await store.contracts.get("proj-1", "src/app.py") is None


async def test_path_escape_is_rejected_and_task_fails(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    payload = {
        "summary": "Tried to write outside the workspace",
        "done": True,
        "changed_files": ["../secret.py"],
        "tool_calls": [
            {
                "name": "write_file",
                "arguments": {"path": "../secret.py", "contents": "nope\n"},
            }
        ],
    }
    _provider, agent = _agent([json.dumps(payload)])
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert result.passed is False
    assert result.tool_outcomes[0].ok is False
    assert not (tmp_path / "secret.py").exists()
    assert result.attempt.result == AttemptResult.FAIL


async def test_failing_tests_keep_validating_despite_done_flag(tmp_path: Path) -> None:
    store, workspace = await _ready_store(
        tmp_path, test_commands=[_failing_test_command()]
    )
    _provider, agent = _agent([json.dumps(_write_app_payload())])
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert result.passed is False
    assert result.project.state == ProjectState.VALIDATING
    assert result.attempt.result == AttemptResult.FAIL
    assert result.worker_result is not None
    assert result.worker_result.done is True
    assert (workspace.root / "src" / "app.py").is_file()
    assert result.attempt.test_log_path is not None
    assert (workspace.root / result.attempt.test_log_path).is_file()


async def test_worker_turn_loop_read_then_write(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "app.py").write_text("old\n", encoding="utf-8")
    first = {
        "summary": "Need the current file",
        "done": False,
        "changed_files": [],
        "tool_calls": [
            {"name": "read_file", "arguments": {"path": "src/app.py"}}
        ],
    }
    second = _write_app_payload("new\n")
    provider, agent = _agent([json.dumps(first), json.dumps(second)])
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        max_turns=4,
    )
    assert result.passed is True
    assert (workspace.root / "src" / "app.py").read_text(encoding="utf-8") == "new\n"
    assert len(provider.requests) == 2
    assert "old" in provider.requests[1].messages[-1].content


async def test_schema_repair_then_success(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    _provider, agent = _agent(["not json", json.dumps(_write_app_payload())])
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert result.passed is True
    assert result.attempt.result == AttemptResult.PASS


async def test_inference_failure_is_error_not_project_failed(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    _provider, agent = _agent(["not json"], json_repairs=0)
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert result.passed is False
    assert result.attempt.result == AttemptResult.ERROR
    assert result.project.state == ProjectState.VALIDATING
    assert result.project.state != ProjectState.FAILED


async def test_rejects_non_ready_project(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root, state=ProjectState.PLANNING)
    await store.projects.save(project)
    await store.tasks.save("proj-1", make_task("STEP_001", status=TaskStatus.READY))
    _provider, agent = _agent([json.dumps(_write_app_payload())])
    with pytest.raises(WorkingError, match="not awaiting"):
        await execute_task(project, "STEP_001", store, agent, workspace, now=FROZEN_NOW)
    stored = await store.projects.get("proj-1")
    assert stored is not None
    assert stored.state == ProjectState.PLANNING


async def test_apply_patch_tool_from_worker(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "app.py").write_text("hello\n", encoding="utf-8")
    payload = {
        "summary": "Patched greeting",
        "done": True,
        "changed_files": ["src/app.py"],
        "tool_calls": [
            {
                "name": "apply_patch",
                "arguments": {
                    "diff": (
                        "--- a/src/app.py\n"
                        "+++ b/src/app.py\n"
                        "@@ -1 +1 @@\n"
                        "-hello\n"
                        "+hello world\n"
                    )
                },
            }
        ],
    }
    _provider, agent = _agent([json.dumps(payload)])
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert result.passed is True
    assert (workspace.root / "src" / "app.py").read_text(encoding="utf-8") == "hello world\n"
    assert result.changed_files == ["src/app.py"]


async def test_execute_task_ensures_worker(tmp_path: Path) -> None:
    store, workspace = await _ready_store(tmp_path)
    _provider, agent = _agent([json.dumps(_write_app_payload())])
    services = FakeInferenceServiceManager()
    result = await execute_task(
        (await store.projects.get("proj-1")),
        "STEP_001",
        store,
        agent,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        services=services,
    )
    assert result.passed is True
    assert services.ensured == [AgentRole.WORKER]

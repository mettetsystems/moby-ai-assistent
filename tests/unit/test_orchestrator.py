import json
import sys
from pathlib import Path

import pytest

from mobyai.agents.orchestrator import (
    OrchestratorAgent,
    OrchestratorValidationError,
    validate_orchestrator_output,
)
from mobyai.agents.worker import WorkerAgent
from mobyai.engine.orchestration import (
    OrchestrationError,
    advance_project,
    run_project,
)
from mobyai.execution.workspace import Workspace
from mobyai.inference.fake import FakeInferenceProvider, FakeInferenceServiceManager
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.attempt import AttemptResult
from mobyai.models.checkpoint import CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.orchestrator import OrchestratorDecision, OrchestratorResult, TargetAgent
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import FROZEN_NOW, make_project, make_task

PYTHON = sys.executable


def _exists_command(relative_path: str) -> str:
    code = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({relative_path!r}).is_file() else 1)"
    )
    return f"{PYTHON} -c {code!r}"


def _write_payload(path: str, contents: str) -> dict[str, object]:
    return {
        "summary": f"Wrote {path}",
        "done": True,
        "changed_files": [path],
        "tool_calls": [
            {"name": "write_file", "arguments": {"path": path, "contents": contents}}
        ],
    }


def _empty_payload() -> dict[str, object]:
    return {
        "summary": "Claimed success without tools",
        "done": True,
        "changed_files": ["src/app.py"],
        "tool_calls": [],
    }


def _retry_payload(
    instruction: str = "Write src/app.py so the acceptance test can pass.",
) -> dict[str, object]:
    return {
        "decision_summary": "Missing target file.",
        "decision": "RETRY_WITH_FIX",
        "task_id": "STEP_001",
        "next_payload": {
            "target_agent": "WORKER",
            "instruction": instruction,
            "required_files": ["src/app.py"],
        },
    }


def _escalate_payload() -> dict[str, object]:
    return {
        "decision_summary": "The same file is still missing after retry.",
        "decision": "ESCALATE_TO_PLANNER",
        "task_id": "STEP_001",
        "next_payload": {
            "target_agent": "PLANNER",
            "instruction": "Rescope STEP_001 into a smaller file-create task.",
            "required_files": ["src/app.py"],
        },
    }


def _worker(responses: list[str]) -> tuple[FakeInferenceProvider, WorkerAgent]:
    provider = FakeInferenceProvider(responses)
    return provider, WorkerAgent(StructuredGenerator(provider, max_repair_attempts=2))


def _orchestrator(
    responses: list[str],
    *,
    json_repairs: int = 2,
    decision_repairs: int = 2,
) -> tuple[FakeInferenceProvider, OrchestratorAgent]:
    provider = FakeInferenceProvider(responses)
    generator = StructuredGenerator(provider, max_repair_attempts=json_repairs)
    return provider, OrchestratorAgent(generator, max_decision_repairs=decision_repairs)


async def _ready_project(
    tmp_path: Path,
    *,
    extra_tasks: list | None = None,
) -> tuple[InMemoryStateStore, Workspace]:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root, state=ProjectState.READY)
    await store.projects.save(project)
    await store.tasks.save(
        project.project_id,
        make_task(
            "STEP_001",
            status=TaskStatus.READY,
            target_files=["src/app.py"],
            acceptance_criteria=["src/app.py exists"],
            test_commands=[_exists_command("src/app.py")],
        ),
    )
    for task in extra_tasks or []:
        await store.tasks.save(project.project_id, task)
    return store, workspace


@pytest.mark.parametrize('command,category', [
    ('source .venv/bin/activate && pip list | grep torch', 'command_syntax'),
    ('.venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000', 'service_lifecycle'),
])
async def test_invalid_saved_command_escalates_on_first_attempt(tmp_path, command, category):
    store, workspace = await _ready_project(tmp_path)
    task = await store.tasks.get('proj-1', 'STEP_001')
    await store.tasks.save('proj-1', task.model_copy(update={
        'test_commands': [command],
    }))
    worker_provider, worker = _worker([])
    orchestrator_provider, orchestrator = _orchestrator([])
    result = await advance_project(await store.projects.get('proj-1'), store,
                                   worker, orchestrator, workspace, now=FROZEN_NOW)
    assert result.project.state == ProjectState.ESCALATING
    assert result.decision == OrchestratorDecision.ESCALATE_TO_PLANNER
    assert (await store.tasks.get('proj-1', 'STEP_001')).attempt_count == 1
    assert not worker_provider.requests
    assert not orchestrator_provider.requests
    events = await store.events.list_for_project('proj-1')
    assert not any(event.event == 'retry_scheduled' for event in events)
    assert any(event.event == 'failure_diagnosed' and
               event.payload['category'] == category for event in events)


def test_validate_orchestrator_rejects_wrong_decision() -> None:
    result = OrchestratorResult.model_validate(
        {
            "decision_summary": "Skip ahead.",
            "decision": "CONTINUE",
            "task_id": "STEP_002",
        }
    )
    with pytest.raises(OrchestratorValidationError, match="RETRY_WITH_FIX"):
        validate_orchestrator_output(
            result,
            expected=OrchestratorDecision.RETRY_WITH_FIX,
            task_id="STEP_001",
        )


def test_validate_orchestrator_requires_worker_payload_on_retry() -> None:
    result = OrchestratorResult.model_validate(
        {
            "decision_summary": "Retry.",
            "decision": "RETRY_WITH_FIX",
            "task_id": "STEP_001",
        }
    )
    with pytest.raises(OrchestratorValidationError, match="next_payload"):
        validate_orchestrator_output(
            result,
            expected=OrchestratorDecision.RETRY_WITH_FIX,
            task_id="STEP_001",
        )


async def test_mission_complete_does_not_call_orchestrator(tmp_path: Path) -> None:
    store, workspace = await _ready_project(tmp_path)
    await store.tasks.save(
        "proj-1",
        make_task("STEP_001", status=TaskStatus.PASSED, target_files=["src/app.py"]),
    )
    orch_provider, orchestrator = _orchestrator([])
    _worker_provider, worker = _worker([])
    step = await advance_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
    )
    assert step.decision is OrchestratorDecision.MISSION_COMPLETE
    assert step.project.state == ProjectState.COMPLETED
    assert orch_provider.requests == []
    assert _worker_provider.requests == []


async def test_selects_ready_task_in_dag_order(tmp_path: Path) -> None:
    store, workspace = await _ready_project(
        tmp_path,
        extra_tasks=[
            make_task(
                "STEP_002",
                status=TaskStatus.READY,
                target_files=["src/other.py"],
                test_commands=[_exists_command("src/other.py")],
                acceptance_criteria=["other exists"],
            )
        ],
    )
    orch_provider, orchestrator = _orchestrator([])
    worker_provider, worker = _worker(
        [json.dumps(_write_payload("src/app.py", "ok\n"))]
    )
    step = await advance_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert step.decision is OrchestratorDecision.CONTINUE
    assert step.execution is not None
    assert step.execution.task.task_id == "STEP_001"
    assert (await store.tasks.get("proj-1", "STEP_001")).status == TaskStatus.PASSED
    assert (await store.tasks.get("proj-1", "STEP_002")).status == TaskStatus.READY
    assert orch_provider.requests == []
    assert len(worker_provider.requests) == 1


async def test_first_failure_retries_with_orchestrator_instruction(tmp_path: Path) -> None:
    store, workspace = await _ready_project(tmp_path)
    instruction = "Create src/app.py with a valid module body."
    orch_provider, orchestrator = _orchestrator([json.dumps(_retry_payload(instruction))])
    worker_provider, worker = _worker(
        [
            json.dumps(_empty_payload()),
            json.dumps(_write_payload("src/app.py", "ok\n")),
        ]
    )
    services = FakeInferenceServiceManager()
    step = await advance_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        services=services,
    )
    assert step.decision is OrchestratorDecision.RETRY_WITH_FIX
    assert step.project.state == ProjectState.RETRYING
    task = await store.tasks.get("proj-1", "STEP_001")
    assert task is not None
    assert task.status == TaskStatus.READY
    assert task.attempt_count == 1
    assert "src/app.py" in task.required_context
    checkpoints = await store.checkpoints.list("proj-1")
    assert any(item.kind is CheckpointKind.RETRY for item in checkpoints)
    assert "RETRY_WITH_FIX" in orch_provider.requests[0].messages[1].content
    assert services.ensured == [AgentRole.WORKER, AgentRole.ORCHESTRATOR]

    retried = await advance_project(
        step.project,
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert retried.decision is OrchestratorDecision.MISSION_COMPLETE
    assert retried.execution is not None
    assert retried.execution.passed is True
    assert instruction in worker_provider.requests[1].messages[1].content
    stored = await store.projects.get("proj-1")
    assert stored is not None
    assert stored.state == ProjectState.COMPLETED


async def test_wrong_orchestrator_decision_is_repaired(tmp_path: Path) -> None:
    store, workspace = await _ready_project(tmp_path)
    wrong = {
        "decision_summary": "Move on.",
        "decision": "CONTINUE",
        "task_id": "STEP_002",
    }
    orch_provider, orchestrator = _orchestrator(
        [json.dumps(wrong), json.dumps(_retry_payload())]
    )
    _provider, worker = _worker([json.dumps(_empty_payload())])
    step = await advance_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert step.decision is OrchestratorDecision.RETRY_WITH_FIX
    assert step.project.state == ProjectState.RETRYING
    assert len(orch_provider.requests) == 2
    assert "RETRY_WITH_FIX" in orch_provider.requests[1].messages[-1].content
    assert step.orchestrator_result is not None
    assert step.orchestrator_result.task_id == "STEP_001"
    assert step.orchestrator_result.next_payload is not None
    assert step.orchestrator_result.next_payload.target_agent is TargetAgent.WORKER


async def test_second_failure_escalates_and_blocks_dependents(tmp_path: Path) -> None:
    store, workspace = await _ready_project(
        tmp_path,
        extra_tasks=[
            make_task(
                "STEP_002",
                dependencies=["STEP_001"],
                target_files=["src/other.py"],
                test_commands=[_exists_command("src/other.py")],
                acceptance_criteria=["other exists"],
            )
        ],
    )
    _orch_provider, orchestrator = _orchestrator(
        [json.dumps(_retry_payload()), json.dumps(_escalate_payload())]
    )
    _worker_provider, worker = _worker(
        [json.dumps(_empty_payload()), json.dumps(_empty_payload())]
    )
    first = await advance_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        max_task_attempts=2,
    )
    assert first.project.state == ProjectState.RETRYING
    second = await advance_project(
        first.project,
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        max_task_attempts=2,
    )
    assert second.decision is OrchestratorDecision.ESCALATE_TO_PLANNER
    assert second.project.state == ProjectState.ESCALATING
    assert (await store.tasks.get("proj-1", "STEP_001")).status == TaskStatus.FAILED
    assert (await store.tasks.get("proj-1", "STEP_002")).status == TaskStatus.BLOCKED
    assert second.escalation is not None
    assert second.escalation.failed_task.task_id == "STEP_001"
    assert "STEP_002" in second.escalation.affected_dependencies
    kinds = [item.kind for item in await store.checkpoints.list("proj-1")]
    assert CheckpointKind.ESCALATION in kinds
    attempts = await store.attempts.list_for_task("proj-1", "STEP_001")
    assert [item.result for item in attempts] == [AttemptResult.FAIL, AttemptResult.FAIL]


async def test_run_project_completes_two_passing_tasks(tmp_path: Path) -> None:
    store, workspace = await _ready_project(
        tmp_path,
        extra_tasks=[
            make_task(
                "STEP_002",
                dependencies=["STEP_001"],
                target_files=["src/other.py"],
                test_commands=[_exists_command("src/other.py")],
                acceptance_criteria=["other exists"],
            )
        ],
    )
    orch_provider, orchestrator = _orchestrator([])
    _provider, worker = _worker(
        [
            json.dumps(_write_payload("src/app.py", "ok\n")),
            json.dumps(_write_payload("src/other.py", "ok\n")),
        ]
    )
    step = await run_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert step.decision is OrchestratorDecision.MISSION_COMPLETE
    assert step.project.state == ProjectState.COMPLETED
    assert orch_provider.requests == []
    assert (await store.tasks.get("proj-1", "STEP_002")).status == TaskStatus.PASSED


async def test_blocked_when_no_ready_tasks(tmp_path: Path) -> None:
    store, workspace = await _ready_project(
        tmp_path,
        extra_tasks=[make_task("STEP_002", dependencies=["STEP_001"])],
    )
    await store.tasks.save(
        "proj-1",
        make_task(
            "STEP_001",
            status=TaskStatus.FAILED,
            target_files=["src/app.py"],
            test_commands=[_exists_command("src/app.py")],
            acceptance_criteria=["exists"],
        ),
    )
    orch_provider, orchestrator = _orchestrator([])
    _provider, worker = _worker([])
    step = await advance_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
    )
    assert step.decision is OrchestratorDecision.BLOCKED
    assert step.project.state == ProjectState.READY
    assert step.project.last_error is not None
    assert "STEP_002" in step.project.last_error
    assert orch_provider.requests == []


async def test_orchestrator_outage_still_retries(tmp_path: Path) -> None:
    store, workspace = await _ready_project(tmp_path)
    _orch_provider, orchestrator = _orchestrator(["not json"], json_repairs=0, decision_repairs=0)
    _provider, worker = _worker([json.dumps(_empty_payload())])
    step = await advance_project(
        (await store.projects.get("proj-1")),
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert step.decision is OrchestratorDecision.RETRY_WITH_FIX
    assert step.project.state == ProjectState.RETRYING
    assert step.orchestrator_result is not None
    assert "fallback" in step.orchestrator_result.decision_summary.lower()


async def test_rejects_planning_state(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root, state=ProjectState.PLANNING)
    await store.projects.save(project)
    _orch, orchestrator = _orchestrator([])
    _w, worker = _worker([])
    with pytest.raises(OrchestrationError, match="not awaiting"):
        await advance_project(project, store, worker, orchestrator, workspace, now=FROZEN_NOW)

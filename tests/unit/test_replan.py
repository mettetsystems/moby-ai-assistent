import json
import sys
from pathlib import Path

import pytest

from mobyai.agents.orchestrator import OrchestratorAgent
from mobyai.agents.planner import PlanValidationError, PlannerAgent, validate_dag_patch
from mobyai.agents.worker import WorkerAgent
from mobyai.engine.orchestration import run_project
from mobyai.engine.replanning import ReplanError, load_escalation_package, replan_project
from mobyai.execution.workspace import Workspace
from mobyai.inference.fake import FakeInferenceProvider, FakeInferenceServiceManager
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.contract import ModuleContract
from mobyai.models.escalation import EscalationPackage
from mobyai.models.orchestrator import OrchestratorDecision
from mobyai.models.planner import DAGPatch
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


def _spec(task_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": task_id,
        "name": task_id,
        "description": f"Atomic work for {task_id}",
        "target_files": ["src/app.py"],
        "acceptance_criteria": ["src/app.py exists"],
        "test_files": [],
        "test_commands": [_exists_command("src/app.py")],
        "dependencies": [],
        "required_context": [],
    }
    payload.update(overrides)
    return payload


def _patch_payload() -> dict[str, object]:
    return {
        "summary": "Split the failed create-file task into a smaller one.",
        "architecture_summary": None,
        "directory_structure": ["src/"],
        "supersede": [
            {
                "task_id": "STEP_001",
                "replacements": [_spec("STEP_001B")],
            }
        ],
        "add_tasks": [],
    }


def _planner(responses: list[str], *, repairs: int = 2) -> tuple[FakeInferenceProvider, PlannerAgent]:
    provider = FakeInferenceProvider(responses)
    generator = StructuredGenerator(provider, max_repair_attempts=repairs)
    return provider, PlannerAgent(generator, max_plan_repairs=repairs)


async def _escalated_store(tmp_path: Path) -> tuple[InMemoryStateStore, Workspace]:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    store = InMemoryStateStore()
    failed = make_task(
        "STEP_001",
        status=TaskStatus.FAILED,
        attempt_count=2,
        target_files=["src/app.py"],
        acceptance_criteria=["src/app.py exists"],
        test_commands=[_exists_command("src/app.py")],
    )
    project = make_project(
        workspace_root=workspace.root,
        state=ProjectState.ESCALATING,
        current_task_id="STEP_001",
        last_error="Missing required files: src/app.py",
        architecture_summary="Small example app.",
    )
    await store.projects.save(project)
    await store.tasks.save_all(
        project.project_id,
        [
            make_task("STEP_000", status=TaskStatus.PASSED, name="Scaffold"),
            failed,
            make_task(
                "STEP_002",
                status=TaskStatus.BLOCKED,
                dependencies=["STEP_001"],
                target_files=["src/other.py"],
                test_commands=[_exists_command("src/other.py")],
                acceptance_criteria=["other exists"],
            ),
        ],
    )
    package = EscalationPackage(
        failed_task=failed,
        affected_dependencies=["STEP_002"],
        affected_files=["src/app.py"],
        last_error=project.last_error,
        decision_summary="Missing target file after retry.",
    )
    await store.checkpoints.save(
        Checkpoint(
            checkpoint_id="proj-1-STEP_001-escalation-2",
            project_id="proj-1",
            kind=CheckpointKind.ESCALATION,
            created_at=FROZEN_NOW,
            payload=package.model_dump(mode="json"),
        )
    )
    return store, workspace


def test_validate_dag_patch_rejects_missing_tests() -> None:
    tasks = [make_task("STEP_001", status=TaskStatus.FAILED)]
    patch = DAGPatch.model_validate(
        {
            "summary": "retry",
            "supersede": [
                {
                    "task_id": "STEP_001",
                    "replacements": [
                        {
                            "task_id": "STEP_001B",
                            "name": "fix",
                            "description": "fix it",
                            "acceptance_criteria": ["ok"],
                            "test_commands": [],
                        }
                    ],
                }
            ],
        }
    )
    with pytest.raises(PlanValidationError, match="test_commands"):
        validate_dag_patch(patch, tasks, failed_task_id="STEP_001")


async def test_replan_patches_branch_and_preserves_passed(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    provider, agent = _planner([json.dumps(_patch_payload())])
    project = await store.projects.get("proj-1")
    assert project is not None

    result = await replan_project(
        project, store, agent, workspace=workspace, now=FROZEN_NOW
    )

    assert result.project.state == ProjectState.READY
    assert result.project.current_task_id is None
    assert result.project.last_error is None
    by_id = {task.task_id: task for task in result.tasks}
    assert by_id["STEP_000"].status == TaskStatus.PASSED
    assert by_id["STEP_001"].status == TaskStatus.SUPERSEDED
    assert by_id["STEP_001B"].status == TaskStatus.READY
    assert by_id["STEP_002"].status == TaskStatus.PENDING
    assert by_id["STEP_002"].dependencies == ["STEP_001B"]
    assert (workspace.root / "src").is_dir()
    loaded = await load_escalation_package(store, "proj-1")
    assert loaded.failed_task.task_id == "STEP_001"
    kinds = [item.kind for item in await store.checkpoints.list("proj-1")]
    assert CheckpointKind.REPLAN in kinds
    events = await store.events.list_for_project("proj-1")
    assert events[-1].event == "dag_replanned"
    assert "STEP_001" in provider.requests[0].messages[1].content
    assert "PASSED" in provider.requests[0].messages[1].content


async def test_replan_includes_module_contracts(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    await store.contracts.upsert(
        ModuleContract(
            project_id="proj-1",
            path="src/db.py",
            task_id="STEP_000",
            rendered="src/db.py\ndef connect() -> int",
            payload={"functions": [{"name": "connect", "args": [], "returns": "int"}]},
            updated_at=FROZEN_NOW,
        )
    )
    provider, agent = _planner([json.dumps(_patch_payload())])
    project = await store.projects.get("proj-1")
    assert project is not None
    await replan_project(project, store, agent, workspace=workspace, now=FROZEN_NOW)
    assert "def connect() -> int" in provider.requests[0].messages[1].content


async def test_invalid_patch_is_repaired(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    bad = {
        "summary": "touch passed work",
        "supersede": [
            {
                "task_id": "STEP_000",
                "replacements": [_spec("STEP_000B")],
            }
        ],
    }
    provider, agent = _planner([json.dumps(bad), json.dumps(_patch_payload())])
    result = await replan_project(
        (await store.projects.get("proj-1")),
        store,
        agent,
        workspace=workspace,
        now=FROZEN_NOW,
    )
    assert result.project.state == ProjectState.READY
    assert len(provider.requests) == 2
    assert "PASSED" in provider.requests[1].messages[-1].content


async def test_replan_failure_marks_failed(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    _provider, agent = _planner(["not json"], repairs=0)
    with pytest.raises(ReplanError):
        await replan_project(
            (await store.projects.get("proj-1")),
            store,
            agent,
            workspace=workspace,
            now=FROZEN_NOW,
        )
    stored = await store.projects.get("proj-1")
    assert stored is not None
    assert stored.state == ProjectState.FAILED
    assert stored.last_error is not None


async def test_rejects_ready_project(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    project = make_project(workspace_root=workspace.root, state=ProjectState.READY)
    await store.projects.save(project)
    _provider, agent = _planner([json.dumps(_patch_payload())])
    with pytest.raises(ReplanError, match="not awaiting"):
        await replan_project(project, store, agent, workspace=workspace, now=FROZEN_NOW)


async def test_resume_from_replanning(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    project = (await store.projects.get("proj-1")).model_copy(
        update={"state": ProjectState.REPLANNING}
    )
    await store.projects.save(project)
    _provider, agent = _planner([json.dumps(_patch_payload())])
    result = await replan_project(project, store, agent, workspace=workspace, now=FROZEN_NOW)
    assert result.project.state == ProjectState.READY


async def test_replan_ensures_planner(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    _provider, agent = _planner([json.dumps(_patch_payload())])
    services = FakeInferenceServiceManager()
    result = await replan_project(
        (await store.projects.get("proj-1")),
        store,
        agent,
        workspace=workspace,
        now=FROZEN_NOW,
        services=services,
    )
    assert result.project.state == ProjectState.READY
    assert services.ensured == [AgentRole.PLANNER]


async def test_run_project_replans_then_completes(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    # Start from READY with the original failing task still runnable so the
    # loop can escalate, then replan, then finish the new branch.
    ready = make_project(workspace_root=workspace.root, state=ProjectState.READY)
    await store.projects.save(ready)
    await store.tasks.save(
        "proj-1",
        make_task(
            "STEP_001",
            status=TaskStatus.READY,
            attempt_count=0,
            target_files=["src/app.py"],
            acceptance_criteria=["src/app.py exists"],
            test_commands=[_exists_command("src/app.py")],
        ),
    )
    await store.tasks.save(
        "proj-1",
        make_task(
            "STEP_002",
            status=TaskStatus.PENDING,
            dependencies=["STEP_001"],
            target_files=["src/other.py"],
            test_commands=[_exists_command("src/other.py")],
            acceptance_criteria=["other exists"],
        ),
    )

    def _write(path: str) -> dict[str, object]:
        return {
            "summary": f"Wrote {path}",
            "done": True,
            "changed_files": [path],
            "tool_calls": [
                {"name": "write_file", "arguments": {"path": path, "contents": "ok\n"}}
            ],
        }

    empty = {
        "summary": "No tools",
        "done": True,
        "changed_files": ["src/app.py"],
        "tool_calls": [],
    }
    retry = {
        "decision_summary": "Missing file.",
        "decision": "RETRY_WITH_FIX",
        "task_id": "STEP_001",
        "next_payload": {
            "target_agent": "WORKER",
            "instruction": "Create src/app.py",
            "required_files": ["src/app.py"],
        },
    }
    escalate = {
        "decision_summary": "Still missing.",
        "decision": "ESCALATE_TO_PLANNER",
        "task_id": "STEP_001",
        "next_payload": {
            "target_agent": "PLANNER",
            "instruction": "Rescope STEP_001",
            "required_files": ["src/app.py"],
        },
    }
    worker_provider = FakeInferenceProvider(
        [
            json.dumps(empty),
            json.dumps(empty),
            json.dumps(_write("src/app.py")),
            json.dumps(_write("src/other.py")),
        ]
    )
    worker = WorkerAgent(StructuredGenerator(worker_provider, max_repair_attempts=2))
    orch_provider = FakeInferenceProvider([json.dumps(retry), json.dumps(escalate)])
    orchestrator = OrchestratorAgent(StructuredGenerator(orch_provider, max_repair_attempts=2))
    _pp, planner = _planner([json.dumps(_patch_payload())])

    step = await run_project(
        ready,
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        planner=planner,
        max_steps=10,
    )
    assert step.decision is OrchestratorDecision.MISSION_COMPLETE
    assert step.project.state == ProjectState.COMPLETED
    assert (await store.tasks.get("proj-1", "STEP_001")).status == TaskStatus.SUPERSEDED
    assert (await store.tasks.get("proj-1", "STEP_001B")).status == TaskStatus.PASSED
    assert (await store.tasks.get("proj-1", "STEP_002")).status == TaskStatus.PASSED
    assert (await store.tasks.get("proj-1", "STEP_000")).status == TaskStatus.PASSED


async def test_replan_inspects_then_patches(tmp_path: Path) -> None:
    store, workspace = await _escalated_store(tmp_path)
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "app.py").write_text("BROKEN = True\n", encoding="utf-8")
    inspect = {
        "inspect_calls": [
            {"name": "read_file", "arguments": {"path": "src/app.py"}},
        ]
    }
    provider, agent = _planner([json.dumps(inspect), json.dumps(_patch_payload())])
    project = await store.projects.get("proj-1")
    assert project is not None
    result = await replan_project(
        project, store, agent, workspace=workspace, now=FROZEN_NOW
    )
    assert result.project.state == ProjectState.READY
    assert "BROKEN" in provider.requests[1].messages[-1].content
    assert (workspace.root / "src" / "app.py").read_text(encoding="utf-8") == "BROKEN = True\n"

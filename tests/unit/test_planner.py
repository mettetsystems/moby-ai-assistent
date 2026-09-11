import json
from pathlib import Path

import pytest

from mobyai.agents.planner import PlanValidationError, PlannerAgent, validate_planner_output
from mobyai.engine.planning import PlanningError, plan_project
from mobyai.execution.workspace import Workspace
from mobyai.inference.fake import FakeInferenceProvider, FakeInferenceServiceManager
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.checkpoint import CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.contract import ModuleContract
from mobyai.models.planner import PlannerOutput
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import FROZEN_NOW, make_project

VALID_PLAN = {
    "project_name": "example_app",
    "architecture_summary": "Small SQLite-backed service.",
    "directory_structure": ["src/", "src/db.py", "tests/", "tests/test_db.py"],
    "tasks": [
        {
            "task_id": "STEP_001",
            "name": "Implement database connection",
            "description": "Create a pooled SQLite connection layer.",
            "target_files": ["src/db.py"],
            "dependencies": [],
            "acceptance_criteria": ["Connection can be created."],
            "test_files": ["tests/test_db.py"],
            "test_commands": ["pytest tests/test_db.py"],
        },
        {
            "task_id": "STEP_002",
            "name": "Add reconnect tests",
            "description": "Cover reconnect-after-failure behavior.",
            "target_files": ["tests/test_db.py"],
            "dependencies": ["STEP_001"],
            "acceptance_criteria": ["Reconnect after failure is tested."],
            "test_files": ["tests/test_db.py"],
            "test_commands": ["pytest tests/test_db.py"],
        },
    ],
    "documentation_requirements": ["README.md"],
}

CYCLIC_PLAN = {
    **VALID_PLAN,
    "tasks": [
        {
            **VALID_PLAN["tasks"][0],
            "dependencies": ["STEP_002"],
        },
        {
            **VALID_PLAN["tasks"][1],
            "dependencies": ["STEP_001"],
        },
    ],
}


def _agent(
    responses: list[str],
    *,
    json_repairs: int = 2,
    plan_repairs: int = 2,
    max_inspect_turns: int = 3,
) -> tuple[FakeInferenceProvider, PlannerAgent]:
    provider = FakeInferenceProvider(responses)
    generator = StructuredGenerator(provider, max_repair_attempts=json_repairs)
    return provider, PlannerAgent(
        generator,
        max_plan_repairs=plan_repairs,
        max_inspect_turns=max_inspect_turns,
    )


def test_validate_planner_output_rejects_cycle() -> None:
    plan = PlannerOutput.model_validate(CYCLIC_PLAN)
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_planner_output(plan)


def test_validate_planner_output_rejects_missing_tests() -> None:
    payload = json.loads(json.dumps(VALID_PLAN))
    payload["tasks"][0]["test_commands"] = []
    plan = PlannerOutput.model_validate(payload)
    with pytest.raises(PlanValidationError, match="test_commands"):
        validate_planner_output(plan)


def test_validate_planner_output_rejects_path_escape() -> None:
    payload = json.loads(json.dumps(VALID_PLAN))
    payload["tasks"][0]["target_files"] = ["../secret.py"]
    plan = PlannerOutput.model_validate(payload)
    with pytest.raises(PlanValidationError, match="non-relative"):
        validate_planner_output(plan)


async def test_plan_project_persists_ready_dag(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    workspace = Workspace(tmp_path / "project")
    _provider, agent = _agent([json.dumps(VALID_PLAN)])
    project = make_project(workspace_root=workspace.root)
    await store.projects.save(project)

    ready, plan = await plan_project(
        project, store, agent, workspace=workspace, now=FROZEN_NOW
    )

    assert ready.state == ProjectState.READY
    assert ready.architecture_summary == "Small SQLite-backed service."
    assert plan.tasks[0].task_id == "STEP_001"
    snapshot = await store.load_snapshot("proj-1")
    assert snapshot is not None
    assert [task.task_id for task in snapshot.tasks] == ["STEP_001", "STEP_002"]
    assert snapshot.tasks[0].status == TaskStatus.READY
    assert snapshot.tasks[1].status == TaskStatus.PENDING
    assert snapshot.latest_checkpoint is not None
    assert snapshot.latest_checkpoint.kind == CheckpointKind.PLAN_CREATED
    assert (workspace.root / "src").is_dir()
    assert (workspace.root / "tests").is_dir()
    events = await store.events.list_for_project("proj-1")
    assert events[0].event == "plan_created"


async def test_plan_project_includes_existing_tree_in_prompt(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "legacy.py").write_text("x = 1\n", encoding="utf-8")
    provider, agent = _agent([json.dumps(VALID_PLAN)])
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root)
    await store.projects.save(project)

    await plan_project(project, store, agent, workspace=workspace, now=FROZEN_NOW)

    user_text = provider.requests[0].messages[1].content
    assert "src/legacy.py" in user_text


async def test_plan_project_repairs_invalid_dag() -> None:
    store = InMemoryStateStore()
    provider, agent = _agent([json.dumps(CYCLIC_PLAN), json.dumps(VALID_PLAN)])
    project = make_project()
    await store.projects.save(project)

    ready, plan = await plan_project(project, store, agent, now=FROZEN_NOW)

    assert ready.state == ProjectState.READY
    assert plan.tasks[1].dependencies == ["STEP_001"]
    assert len(provider.requests) == 2
    assert "cycle" in provider.requests[1].messages[-1].content.lower()


async def test_plan_project_failure_marks_failed() -> None:
    store = InMemoryStateStore()
    _provider, agent = _agent(["not json"], json_repairs=0, plan_repairs=0)
    project = make_project()
    await store.projects.save(project)

    with pytest.raises(PlanningError):
        await plan_project(project, store, agent, now=FROZEN_NOW)

    stored = await store.projects.get("proj-1")
    assert stored is not None
    assert stored.state == ProjectState.FAILED
    assert stored.last_error is not None
    assert await store.tasks.list_for_project("proj-1") == []


async def test_plan_project_rejects_non_new_state() -> None:
    store = InMemoryStateStore()
    _provider, agent = _agent([json.dumps(VALID_PLAN)])
    project = make_project(state=ProjectState.READY)
    await store.projects.save(project)
    with pytest.raises(PlanningError, match="not awaiting"):
        await plan_project(project, store, agent, now=FROZEN_NOW)
    stored = await store.projects.get("proj-1")
    assert stored is not None
    assert stored.state == ProjectState.READY


async def test_resume_planning_from_planning_state() -> None:
    store = InMemoryStateStore()
    _provider, agent = _agent([json.dumps(VALID_PLAN)])
    project = make_project(state=ProjectState.PLANNING)
    await store.projects.save(project)
    ready, _plan = await plan_project(project, store, agent, now=FROZEN_NOW)
    assert ready.state == ProjectState.READY


async def test_plan_project_ensures_planner_after_persist() -> None:
    store = InMemoryStateStore()
    _provider, agent = _agent([json.dumps(VALID_PLAN)])
    project = make_project()
    await store.projects.save(project)
    services = FakeInferenceServiceManager()
    ready, _plan = await plan_project(
        project, store, agent, now=FROZEN_NOW, services=services
    )
    assert ready.state == ProjectState.READY
    assert services.ensured == [AgentRole.PLANNER]


async def test_plan_project_fails_when_planner_service_unhealthy() -> None:
    store = InMemoryStateStore()
    _provider, agent = _agent([json.dumps(VALID_PLAN)])
    project = make_project()
    await store.projects.save(project)
    services = FakeInferenceServiceManager()
    services.fail_roles.add(AgentRole.PLANNER)
    with pytest.raises(PlanningError, match="unhealthy"):
        await plan_project(project, store, agent, now=FROZEN_NOW, services=services)
    stored = await store.projects.get("proj-1")
    assert stored is not None
    assert stored.state == ProjectState.FAILED
    assert services.ensured == [AgentRole.PLANNER]


async def test_plan_project_inspects_then_plans(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "legacy.py").write_text(
        "LEGACY_MARKER = 1\n", encoding="utf-8"
    )
    inspect = {
        "inspect_calls": [
            {"name": "read_file", "arguments": {"path": "src/legacy.py"}},
            {"name": "glob", "arguments": {"pattern": "**/*.py"}},
        ]
    }
    provider, agent = _agent([json.dumps(inspect), json.dumps(VALID_PLAN)])
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root)
    await store.projects.save(project)

    ready, plan = await plan_project(
        project, store, agent, workspace=workspace, now=FROZEN_NOW
    )

    assert ready.state == ProjectState.READY
    assert plan.tasks[0].task_id == "STEP_001"
    assert len(provider.requests) == 2
    feedback = provider.requests[1].messages[-1].content
    assert "LEGACY_MARKER" in feedback
    assert "src/legacy.py" in feedback
    assert (workspace.root / "src" / "legacy.py").read_text(
        encoding="utf-8"
    ) == "LEGACY_MARKER = 1\n"


async def test_plan_project_inspect_budget_then_plans(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    inspect = {
        "inspect_calls": [{"name": "list_directory", "arguments": {"path": "."}}]
    }
    provider, agent = _agent(
        [json.dumps(inspect), json.dumps(inspect), json.dumps(VALID_PLAN)],
        max_inspect_turns=1,
    )
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root)
    await store.projects.save(project)

    ready, plan = await plan_project(
        project, store, agent, workspace=workspace, now=FROZEN_NOW
    )

    assert ready.state == ProjectState.READY
    assert plan.project_name == "example_app"
    assert len(provider.requests) == 3
    assert "inspect is finished" in provider.requests[2].messages[-1].content.lower()


async def test_plan_project_includes_module_contracts_in_prompt(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "project")
    workspace.ensure_root()
    store = InMemoryStateStore()
    project = make_project(workspace_root=workspace.root)
    await store.projects.save(project)
    from mobyai.context.contracts import source_digest
    source = "def connect() -> int:\n    return 1\n"
    (workspace.root / "src").mkdir(exist_ok=True)
    (workspace.root / "src/db.py").write_text(source)
    await store.contracts.upsert(
        ModuleContract(
            project_id=project.project_id,
            path="src/db.py",
            task_id="STEP_000",
            rendered="src/db.py\ndef connect() -> int",
            payload={"source_sha256": source_digest(source)},
            updated_at=FROZEN_NOW,
        )
    )
    provider, agent = _agent([json.dumps(VALID_PLAN)])
    await plan_project(project, store, agent, workspace=workspace, now=FROZEN_NOW)
    user_text = provider.requests[0].messages[1].content
    assert "def connect() -> int" in user_text

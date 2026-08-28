"""End-to-end fixture: REST+SQLite task manager through retry, replan, and resume."""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

import yaml

from mobyai.agents.orchestrator import OrchestratorAgent
from mobyai.agents.planner import PlannerAgent
from mobyai.agents.worker import WorkerAgent
from mobyai.cli import async_main
from mobyai.engine.orchestration import advance_project, run_project
from mobyai.engine.planning import plan_project
from mobyai.engine.recovery import resume_project
from mobyai.execution.workspace import Workspace
from mobyai.inference.fake import FakeInferenceProvider, FakeInferenceServiceManager
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.checkpoint import CheckpointKind
from mobyai.models.config import AgentRole, AppConfig
from mobyai.models.orchestrator import OrchestratorDecision
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.runtime import Runtime
from mobyai.state.sqlite import SqliteStateStore
from tests.fixtures.task_manager.scenario import (
    OBJECTIVE,
    orchestrator_responses,
    planner_responses,
    worker_responses,
)
from tests.helpers import FROZEN_NOW, make_project

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_task_api(workspace: Path) -> None:
    api = _load_module(workspace / "src" / "app.py").TaskAPI()
    created_status, created_body = api.handle("POST", "/tasks", '{"title":"buy milk"}')
    assert created_status == 201
    list_status, list_body = api.handle("GET", "/tasks")
    assert list_status == 200
    assert "buy milk" in list_body


def _config(tmp_path: Path) -> AppConfig:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    payload["persistence"]["database_path"] = str(tmp_path / "mobyai.db")
    payload["project"]["workspace_root"] = str(tmp_path / "projects")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return AppConfig.from_yaml(path)


def _agents() -> tuple[PlannerAgent, OrchestratorAgent, WorkerAgent]:
    repairs = 2
    planner = PlannerAgent(
        StructuredGenerator(
            FakeInferenceProvider(planner_responses()), max_repair_attempts=repairs
        ),
        max_plan_repairs=repairs,
    )
    orchestrator = OrchestratorAgent(
        StructuredGenerator(
            FakeInferenceProvider(orchestrator_responses()),
            max_repair_attempts=repairs,
        ),
        max_decision_repairs=repairs,
    )
    worker = WorkerAgent(
        StructuredGenerator(
            FakeInferenceProvider(worker_responses()), max_repair_attempts=repairs
        )
    )
    return planner, orchestrator, worker


def _runtime(tmp_path: Path) -> tuple[Path, Runtime]:
    config = _config(tmp_path)
    planner, orchestrator, worker = _agents()
    runtime = Runtime(
        config,
        SqliteStateStore(config.persistence.database_path),
        planner,
        orchestrator,
        worker,
        FakeInferenceServiceManager(),
        owns_store=True,
    )
    return tmp_path / "config.yaml", runtime


async def test_task_manager_plans_retries_replans_and_completes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteStateStore(config.persistence.database_path)
    await store.initialize()
    workspace = Workspace(tmp_path / "projects" / "task-manager")
    workspace.ensure_root()
    project = make_project(
        project_id="task-manager",
        name="task_manager",
        objective=OBJECTIVE,
        workspace_root=workspace.root,
        state=ProjectState.NEW,
    )
    await store.projects.save(project)
    planner, orchestrator, worker = _agents()
    services = FakeInferenceServiceManager()

    ready, plan = await plan_project(
        project, store, planner, workspace=workspace, now=FROZEN_NOW, services=services
    )
    assert ready.state is ProjectState.READY
    assert [spec.task_id for spec in plan.tasks] == ["STEP_001", "STEP_002", "STEP_003"]

    step = await run_project(
        ready,
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        planner=planner,
        services=services,
        max_steps=12,
    )

    assert step.decision is OrchestratorDecision.MISSION_COMPLETE
    assert step.project.state is ProjectState.COMPLETED
    by_id = {
        task.task_id: task
        for task in await store.tasks.list_for_project("task-manager")
    }
    assert by_id["STEP_001"].status is TaskStatus.PASSED
    assert by_id["STEP_002"].status is TaskStatus.SUPERSEDED
    assert by_id["STEP_002B"].status is TaskStatus.PASSED
    assert by_id["STEP_003"].status is TaskStatus.PASSED
    assert by_id["STEP_003"].dependencies == ["STEP_002B"]
    assert (workspace.root / "src" / "db.py").is_file()
    assert (workspace.root / "src" / "app.py").is_file()
    assert "TaskAPI" in (workspace.root / "src" / "app.py").read_text(encoding="utf-8")
    assert (workspace.root / "README.md").is_file()
    _assert_task_api(workspace.root)

    kinds = [item.kind for item in await store.checkpoints.list("task-manager")]
    assert CheckpointKind.PLAN_CREATED in kinds
    assert CheckpointKind.RETRY in kinds
    assert CheckpointKind.ESCALATION in kinds
    assert CheckpointKind.REPLAN in kinds
    events = [item.event for item in await store.events.list_for_project("task-manager")]
    assert "plan_created" in events
    assert "task_passed" in events
    assert "dag_replanned" in events
    assert AgentRole.PLANNER in services.ensured
    assert AgentRole.WORKER in services.ensured
    assert AgentRole.ORCHESTRATOR in services.ensured
    await store.close()


async def test_task_manager_survives_sqlite_reconnect(tmp_path: Path) -> None:
    config = _config(tmp_path)
    db_path = config.persistence.database_path
    store = SqliteStateStore(db_path)
    await store.initialize()
    workspace = Workspace(tmp_path / "projects" / "task-manager")
    workspace.ensure_root()
    project = make_project(
        project_id="task-manager",
        name="task_manager",
        objective=OBJECTIVE,
        workspace_root=workspace.root,
        state=ProjectState.NEW,
    )
    await store.projects.save(project)
    planner, orchestrator, worker = _agents()

    ready, _plan = await plan_project(
        project, store, planner, workspace=workspace, now=FROZEN_NOW
    )
    first = await advance_project(
        ready,
        store,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
    )
    assert first.decision is OrchestratorDecision.CONTINUE
    assert first.project.state is ProjectState.READY
    assert (await store.tasks.get("task-manager", "STEP_001")).status is TaskStatus.PASSED
    await store.close()

    restored = SqliteStateStore(db_path)
    await restored.initialize()
    snapshot = await resume_project(restored, "task-manager")
    assert snapshot.project.state is ProjectState.READY
    assert snapshot.latest_checkpoint is not None
    assert any(task.status is TaskStatus.PASSED for task in snapshot.tasks)

    step = await run_project(
        snapshot.project,
        restored,
        worker,
        orchestrator,
        workspace,
        now=FROZEN_NOW,
        command_timeout_seconds=10,
        planner=planner,
        max_steps=12,
    )
    assert step.project.state is ProjectState.COMPLETED
    assert (await restored.tasks.get("task-manager", "STEP_002")).status is TaskStatus.SUPERSEDED
    assert (await restored.tasks.get("task-manager", "STEP_002B")).status is TaskStatus.PASSED
    _assert_task_api(workspace.root)
    await restored.close()


async def test_task_manager_cli_run_completes(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    stdout = StringIO()
    stderr = StringIO()
    init = await async_main(
        [
            "--config",
            str(config_path),
            "init",
            "--name",
            "task-manager",
            "--id",
            "task-manager",
            "--objective",
            OBJECTIVE,
        ],
        stdout=stdout,
        stderr=stderr,
        runtime=runtime,
    )
    assert init == 0
    assert stderr.getvalue() == ""
    run = await async_main(
        ["--config", str(config_path), "run", "task-manager", "--max-steps", "12"],
        stdout=stdout,
        stderr=stderr,
        runtime=runtime,
    )
    assert stderr.getvalue() == ""
    assert run == 0
    assert "COMPLETED" in stdout.getvalue()
    stored = await runtime.store.projects.get("task-manager")
    assert stored is not None
    assert stored.state is ProjectState.COMPLETED
    workspace = tmp_path / "projects" / "task-manager"
    assert (workspace / "src" / "app.py").is_file()
    _assert_task_api(workspace)
    await runtime.aclose()


"""Initial planning loop. The harness owns transitions and persistence."""

from __future__ import annotations

from datetime import UTC, datetime

from mobyai.agents.planner import PlanValidationError, PlannerAgent
from mobyai.dag.graph import TaskGraph
from mobyai.context.contracts import contract_is_current
from mobyai.context.tokenizer import estimate_tokens
from mobyai.engine.state_machine import ProjectStateMachine
from mobyai.execution.errors import WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.tools import ToolDispatcher
from mobyai.execution.workspace import Workspace
from mobyai.inference.base import InferenceError, SchemaRepairExhaustedError
from mobyai.inference.service_manager import InferenceServiceManager
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.event import EventRecord
from mobyai.models.planner import PlannerOutput
from mobyai.models.project import Project, ProjectState
from mobyai.models.snapshot import ProjectSnapshot
from mobyai.state.repository import StateStore

_PLAN_FAILURES = (
    PlanValidationError,
    SchemaRepairExhaustedError,
    InferenceError,
)


class PlanningError(Exception):
    """Initial planning could not complete."""


async def plan_project(
    project: Project,
    store: StateStore,
    agent: PlannerAgent,
    *,
    workspace: Workspace | None = None,
    now: datetime | None = None,
    scaffold: bool = True,
    services: InferenceServiceManager | None = None,
) -> tuple[Project, PlannerOutput]:
    """NEW/PLANNING → READY with a validated DAG, or PLANNING → FAILED."""
    timestamp = now or datetime.now(UTC)
    if project.state not in {ProjectState.NEW, ProjectState.PLANNING}:
        raise PlanningError(
            f"Project {project.project_id} is {project.state}, not awaiting an initial plan"
        )

    if project.state == ProjectState.NEW:
        project = ProjectStateMachine.transition(
            project, ProjectState.PLANNING, now=timestamp
        )
        await store.projects.save(project)

    existing_paths = await _existing_paths(workspace)
    module_contracts = await _module_contract_listings(store, project.project_id, workspace)
    try:
        if services is not None:
            await services.ensure_role_available(AgentRole.PLANNER)
        plan = await agent.create_plan(
            project.objective,
            project_name=project.name,
            existing_paths=existing_paths,
            inspector=_inspector(workspace),
            module_contracts=module_contracts,
        )
    except _PLAN_FAILURES as exc:
        failed = ProjectStateMachine.transition(
            project.model_copy(update={"last_error": str(exc)}),
            ProjectState.FAILED,
            now=timestamp,
        )
        await store.projects.save(failed)
        await _record_event(
            store, failed, "plan_failed", timestamp, {"error": str(exc)}
        )
        raise PlanningError(str(exc)) from exc

    graph = TaskGraph([spec.to_task() for spec in plan.tasks])
    ready = ProjectStateMachine.transition(
        _apply_plan_metadata(project, plan, timestamp),
        ProjectState.PLAN_READY,
        now=timestamp,
    )
    ready = ProjectStateMachine.transition(ready, ProjectState.READY, now=timestamp)
    await store.projects.save(ready)
    await store.tasks.save_all(ready.project_id, graph.tasks())
    await store.checkpoints.save(
        Checkpoint(
            checkpoint_id=f"{ready.project_id}-plan-created",
            project_id=ready.project_id,
            kind=CheckpointKind.PLAN_CREATED,
            created_at=timestamp,
            payload={
                "project_name": plan.project_name,
                "task_ids": [task.task_id for task in graph.tasks()],
            },
        )
    )
    await _record_event(
        store,
        ready,
        "plan_created",
        timestamp,
        {"task_count": len(graph)},
    )
    if scaffold and workspace is not None:
        _scaffold_directories(workspace, plan.directory_structure)
    return ready, plan


async def load_planned_snapshot(store: StateStore, project_id: str) -> ProjectSnapshot:
    snapshot = await store.load_snapshot(project_id)
    if snapshot is None:
        raise PlanningError(f"Project not found: {project_id}")
    return snapshot


async def _existing_paths(workspace: Workspace | None) -> list[str]:
    if workspace is None or not workspace.root.exists():
        return []
    files = WorkspaceFilesystem(workspace)
    try:
        return await files.list_tree(".", max_entries=200)
    except OSError:
        return []


def _inspector(workspace: Workspace | None) -> ToolDispatcher | None:
    if workspace is None or not workspace.root.exists():
        return None
    return ToolDispatcher.from_workspace(workspace)


async def _module_contract_listings(
    store: StateStore, project_id: str, workspace: Workspace | None = None,
    token_budget: int = 8000,
) -> list[str]:
    if workspace is None:
        return []
    files = WorkspaceFilesystem(workspace)
    items = await store.contracts.list_for_project(project_id)
    selected: list[str] = []
    remaining = token_budget
    for item in sorted(items, key=lambda item: item.path):
        try:
            source = await files.read_file(item.path)
        except (OSError, ValueError, WorkspaceError):
            continue
        if not contract_is_current(item, source):
            continue
        cost = estimate_tokens(item.rendered + "\n\n")
        if cost <= remaining:
            selected.append(item.rendered)
            remaining -= cost
    return selected


def _apply_plan_metadata(
    project: Project, plan: PlannerOutput, timestamp: datetime
) -> Project:
    return project.model_copy(
        update={
            "name": plan.project_name,
            "architecture_summary": plan.architecture_summary,
            "directory_structure": list(plan.directory_structure),
            "documentation_requirements": list(plan.documentation_requirements),
            "last_error": None,
            "updated_at": timestamp,
        }
    )


def _scaffold_directories(workspace: Workspace, directory_structure: list[str]) -> None:
    workspace.ensure_root()
    for entry in directory_structure:
        target = workspace.resolve(entry)
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)


async def _record_event(
    store: StateStore,
    project: Project,
    event: str,
    timestamp: datetime,
    payload: dict[str, object],
) -> None:
    await store.events.append(
        EventRecord(
            timestamp=timestamp,
            project_id=project.project_id,
            component="planner",
            event=event,
            payload=payload,
        )
    )

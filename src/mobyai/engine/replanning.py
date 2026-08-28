"""Planner escalation: patch the failed DAG branch. Do not regenerate the whole plan."""

from __future__ import annotations

from datetime import UTC, datetime

from mobyai.agents.planner import PlanValidationError, PlannerAgent
from mobyai.dag.graph import TaskGraph
from mobyai.dag.patch import apply_dag_patch
from mobyai.engine.planning import _existing_paths, _scaffold_directories
from mobyai.engine.state_machine import ProjectStateMachine
from mobyai.execution.workspace import Workspace
from mobyai.inference.base import InferenceError, SchemaRepairExhaustedError
from mobyai.inference.service_manager import InferenceServiceManager
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.escalation import EscalationPackage
from mobyai.models.event import EventRecord
from mobyai.models.planner import DAGPatch
from mobyai.models.project import Project, ProjectState
from mobyai.models.task import Task
from mobyai.state.repository import StateStore

_REPLAN_FAILURES = (
    PlanValidationError,
    SchemaRepairExhaustedError,
    InferenceError,
)


class ReplanError(Exception):
    """Escalation replan could not complete."""


class ReplanResult:
    def __init__(self, *, project: Project, patch: DAGPatch, tasks: list[Task]) -> None:
        self.project = project
        self.patch = patch
        self.tasks = tasks


async def replan_project(
    project: Project,
    store: StateStore,
    agent: PlannerAgent,
    *,
    workspace: Workspace | None = None,
    now: datetime | None = None,
    package: EscalationPackage | None = None,
    services: InferenceServiceManager | None = None,
) -> ReplanResult:
    """ESCALATING/REPLANNING → READY with a branch patch, or REPLANNING → FAILED."""
    timestamp = now or datetime.now(UTC)
    if project.state not in {ProjectState.ESCALATING, ProjectState.REPLANNING}:
        raise ReplanError(
            f"Project {project.project_id} is {project.state}, not awaiting a DAG replan"
        )

    if project.state == ProjectState.ESCALATING:
        project = ProjectStateMachine.transition(
            project, ProjectState.REPLANNING, now=timestamp
        )
        await store.projects.save(project)

    loaded = package or await load_escalation_package(store, project.project_id)
    tasks = await store.tasks.list_for_project(project.project_id)
    existing_paths = await _existing_paths(workspace)
    try:
        if services is not None:
            await services.ensure_role_available(AgentRole.PLANNER)
        patch = await agent.replan(
            objective=project.objective,
            project_name=project.name,
            tasks=tasks,
            package=loaded,
            existing_paths=existing_paths,
        )
    except _REPLAN_FAILURES as exc:
        failed = ProjectStateMachine.transition(
            project.model_copy(update={"last_error": str(exc)}),
            ProjectState.FAILED,
            now=timestamp,
        )
        await store.projects.save(failed)
        await _record_event(
            store, failed, loaded.failed_task.task_id, "replan_failed", timestamp, {"error": str(exc)}
        )
        raise ReplanError(str(exc)) from exc

    patched = apply_dag_patch(tasks, patch, failed_task_id=loaded.failed_task.task_id)
    graph = TaskGraph(patched)
    ready = ProjectStateMachine.transition(
        _apply_patch_metadata(project, patch, timestamp),
        ProjectState.READY,
        now=timestamp,
    )
    await store.projects.save(ready)
    await store.tasks.save_all(ready.project_id, graph.tasks())
    await store.checkpoints.save(
        Checkpoint(
            checkpoint_id=f"{ready.project_id}-replan-{loaded.failed_task.task_id}",
            project_id=ready.project_id,
            kind=CheckpointKind.REPLAN,
            created_at=timestamp,
            payload={
                "failed_task_id": loaded.failed_task.task_id,
                "summary": patch.summary,
                "superseded": [item.task_id for item in patch.supersede],
                "added": [
                    spec.task_id
                    for item in patch.supersede
                    for spec in item.replacements
                ]
                + [spec.task_id for spec in patch.add_tasks],
            },
        )
    )
    await _record_event(
        store,
        ready,
        loaded.failed_task.task_id,
        "dag_replanned",
        timestamp,
        {
            "summary": patch.summary,
            "superseded": [item.task_id for item in patch.supersede],
        },
    )
    if workspace is not None and patch.directory_structure:
        _scaffold_directories(workspace, patch.directory_structure)
    return ReplanResult(project=ready, patch=patch, tasks=graph.tasks())


async def load_escalation_package(store: StateStore, project_id: str) -> EscalationPackage:
    checkpoints = await store.checkpoints.list(project_id)
    for item in reversed(checkpoints):
        if item.kind is CheckpointKind.ESCALATION:
            return EscalationPackage.model_validate(item.payload)
    raise ReplanError(f"No escalation package found for project {project_id}")


def _apply_patch_metadata(project: Project, patch: DAGPatch, timestamp: datetime) -> Project:
    updates: dict[str, object] = {
        "last_error": None,
        "current_task_id": None,
        "updated_at": timestamp,
    }
    if patch.architecture_summary:
        updates["architecture_summary"] = patch.architecture_summary
    return project.model_copy(update=updates)


async def _record_event(
    store: StateStore,
    project: Project,
    task_id: str | None,
    event: str,
    timestamp: datetime,
    payload: dict[str, object],
) -> None:
    await store.events.append(
        EventRecord(
            timestamp=timestamp,
            project_id=project.project_id,
            task_id=task_id,
            component="planner",
            event=event,
            payload=payload,
        )
    )

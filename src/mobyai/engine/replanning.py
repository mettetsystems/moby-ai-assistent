"""Planner escalation: patch the failed DAG branch. Do not regenerate the whole plan."""

from __future__ import annotations

from mobyai.tracing import traced

from datetime import UTC, datetime

from mobyai.agents.planner import PlanValidationError, PlannerAgent
from mobyai.dag.graph import TaskGraph
from mobyai.dag.patch import apply_dag_patch
from mobyai.engine.planning import (
    _existing_paths,
    _inspector,
    _module_contract_listings,
    _scaffold_directories,
)
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
from mobyai.models.task import Task, TaskStatus
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


@traced("planner.replan")
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

    if project.current_task_id is None:
        raise ReplanError("Cannot replan without a current failed task")
    loaded = package or await load_escalation_package(
        store, project.project_id, task_id=project.current_task_id
    )
    from mobyai.engine.timeouts import current_policy
    policy = current_policy()
    if policy is not None:
        loaded = loaded.model_copy(update={"decision_summary":
            (loaded.decision_summary or "") + "\n" + policy.planner_context()})
    if (project.last_error or '').startswith('Planner recovery:'):
        loaded = loaded.model_copy(update={'decision_summary':(loaded.decision_summary or '') + '\nCorrect the previous replan validation error: ' + project.last_error})
    tasks = await store.tasks.list_for_project(project.project_id)
    failed = next((task for task in tasks if task.task_id == project.current_task_id), None)
    if (failed is None or failed.status is not TaskStatus.FAILED
            or loaded.failed_task.task_id != failed.task_id
            or loaded.failed_task.attempt_count != failed.attempt_count):
        raise ReplanError("Escalation package does not match the current failed task attempt")
    history = await store.events.list_for_project(project.project_id)
    recovery_records = [event.payload for event in history if event.event == "recovery_evidence"][-4:]
    if recovery_records:
        import json
        loaded = loaded.model_copy(update={"decision_summary": (loaded.decision_summary or "") +
            "\nRECOVERY EVIDENCE (retain validation; change failed hypotheses): " + json.dumps(recovery_records)})
    existing_paths = await _existing_paths(workspace)
    module_contracts = await _module_contract_listings(store, project.project_id, workspace)
    try:
        if services is not None:
            await services.ensure_role_available(AgentRole.PLANNER)
        from mobyai.research.access import research_access
        from mobyai.engine.environment import planning_environment
        environment = await planning_environment(store, project.project_id, workspace, now=timestamp)
        with research_access(loaded.failed_task.attempt_count >= 2 and '[RESEARCH_REQUIRED]' in (loaded.decision_summary or '')):
            patch = await agent.replan(
                objective=project.objective + environment,
                project_name=project.name,
                tasks=tasks,
                package=loaded,
                existing_paths=existing_paths,
                inspector=_inspector(workspace),
                module_contracts=module_contracts,
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


async def load_escalation_package(
    store: StateStore, project_id: str, *, task_id: str | None = None
) -> EscalationPackage:
    checkpoints = await store.checkpoints.list(project_id)
    for item in reversed(checkpoints):
        if item.kind is CheckpointKind.ESCALATION:
            package = EscalationPackage.model_validate(item.payload)
            if task_id is None or package.failed_task.task_id == task_id:
                return package
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

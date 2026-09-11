"""Orchestrator loop: select, dispatch, evaluate, retry, escalate, complete.

Counters, DAG eligibility, and transitions are application-owned. The Orchestrator
model only writes a remediation or escalation summary.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mobyai.agents.orchestrator import OrchestratorAgent, OrchestratorValidationError
from mobyai.agents.planner import PlannerAgent
from mobyai.agents.worker import WorkerAgent
from mobyai.context.log_reducer import LogReducer, ReducedLog
from mobyai.dag.graph import TaskGraph
from mobyai.engine.state_machine import ProjectStateMachine
from mobyai.engine.replanning import replan_project
from mobyai.engine.working import TaskExecutionResult, execute_task
from mobyai.execution.errors import WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.workspace import Workspace
from mobyai.inference.base import InferenceError, SchemaRepairExhaustedError
from mobyai.inference.service_manager import InferenceServiceManager
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.escalation import EscalationPackage
from mobyai.models.event import EventRecord
from mobyai.models.orchestrator import (
    NextPayload,
    OrchestratorDecision,
    OrchestratorResult,
    TargetAgent,
)
from mobyai.models.project import Project, ProjectState
from mobyai.models.task import Task, TaskStatus
from mobyai.state.repository import StateStore

_DISPATCH_STATES = frozenset(
    {
        ProjectState.READY,
        ProjectState.RETRYING,
        ProjectState.DISPATCHING,
        ProjectState.WORKING,
    }
)
_ORCHESTRATOR_FAILURES = (
    OrchestratorValidationError,
    SchemaRepairExhaustedError,
    InferenceError,
)
_FAILED_ATTEMPTS = frozenset(
    {AttemptResult.FAIL, AttemptResult.ERROR, AttemptResult.TIMEOUT}
)


class OrchestrationError(Exception):
    """The orchestrator loop could not start from the current project state."""


class OrchestrationStep:
    def __init__(
        self,
        *,
        project: Project,
        decision: OrchestratorDecision,
        orchestrator_result: OrchestratorResult | None = None,
        execution: TaskExecutionResult | None = None,
        escalation: EscalationPackage | None = None,
    ) -> None:
        self.project = project
        self.decision = decision
        self.orchestrator_result = orchestrator_result
        self.execution = execution
        self.escalation = escalation


async def advance_project(
    project: Project,
    store: StateStore,
    worker: WorkerAgent,
    orchestrator: OrchestratorAgent,
    workspace: Workspace,
    *,
    max_task_attempts: int = 2,
    token_budget: int = 48000,
    max_turns: int = 8,
    command_timeout_seconds: int = 600,
    now: datetime | None = None,
    services: InferenceServiceManager | None = None,
    prefer_import_contracts: bool = True,
) -> OrchestrationStep:
    """Take one outer step: complete, block, dispatch, retry, or escalate."""
    timestamp = now or datetime.now(UTC)
    if project.state == ProjectState.VALIDATING:
        return await resolve_validation_failure(
            project,
            store,
            orchestrator,
            workspace,
            max_task_attempts=max_task_attempts,
            now=timestamp,
            services=services,
        )
    if project.state not in _DISPATCH_STATES:
        raise OrchestrationError(
            f"Project {project.project_id} is {project.state}, not awaiting orchestration"
        )

    if project.state == ProjectState.READY:
        graph = TaskGraph(await store.tasks.list_for_project(project.project_id))
        if graph.is_complete():
            return await _complete(project, store, timestamp)
        nxt = graph.select_next_task()
        if nxt is None:
            return await _blocked(project, store, graph, timestamp)
        task_id = nxt.task_id
    else:
        task_id = project.current_task_id
        if not task_id:
            raise OrchestrationError(
                f"Project {project.project_id} is {project.state} without current_task_id"
            )

    execution = await execute_task(
        project,
        task_id,
        store,
        worker,
        workspace,
        token_budget=token_budget,
        max_turns=max_turns,
        command_timeout_seconds=command_timeout_seconds,
        now=timestamp,
        services=services,
        prefer_import_contracts=prefer_import_contracts,
    )
    if execution.passed:
        graph = TaskGraph(await store.tasks.list_for_project(project.project_id))
        if graph.is_complete():
            completed = await _complete(execution.project, store, timestamp)
            completed.execution = execution
            return completed
        return OrchestrationStep(
            project=execution.project,
            decision=OrchestratorDecision.CONTINUE,
            execution=execution,
        )
    return await resolve_validation_failure(
        execution.project,
        store,
        orchestrator,
        workspace,
        max_task_attempts=max_task_attempts,
        now=timestamp,
        execution=execution,
        services=services,
    )


async def run_project(
    project: Project,
    store: StateStore,
    worker: WorkerAgent,
    orchestrator: OrchestratorAgent,
    workspace: Workspace,
    *,
    max_task_attempts: int = 2,
    max_steps: int = 50,
    token_budget: int = 48000,
    max_turns: int = 8,
    command_timeout_seconds: int = 600,
    now: datetime | None = None,
    planner: PlannerAgent | None = None,
    max_replans: int = 3,
    services: InferenceServiceManager | None = None,
    prefer_import_contracts: bool = True,
) -> OrchestrationStep:
    """Advance until complete, blocked, escalating, failed, or max_steps."""
    timestamp = now or datetime.now(UTC)
    current = project
    last: OrchestrationStep | None = None
    replans = 0
    for _ in range(max(max_steps, 1)):
        if current.state == ProjectState.ESCALATING:
            if planner is None or replans >= max_replans:
                if last is not None:
                    return last
                raise OrchestrationError(
                    f"Project {project.project_id} is ESCALATING without a Planner"
                )
            result = await replan_project(
                current,
                store,
                planner,
                workspace=workspace,
                now=timestamp,
                services=services,
            )
            current = result.project
            replans += 1
            continue
        last = await advance_project(
            current,
            store,
            worker,
            orchestrator,
            workspace,
            max_task_attempts=max_task_attempts,
            token_budget=token_budget,
            max_turns=max_turns,
            command_timeout_seconds=command_timeout_seconds,
            now=timestamp,
            services=services,
            prefer_import_contracts=prefer_import_contracts,
        )
        current = last.project
        if current.state in {ProjectState.COMPLETED, ProjectState.FAILED}:
            return last
        if last.decision is OrchestratorDecision.BLOCKED:
            return last
        if current.state == ProjectState.ESCALATING and (
            planner is None or replans >= max_replans
        ):
            return last
    raise OrchestrationError(
        f"Project {project.project_id} exceeded {max_steps} orchestration step(s)"
    )


async def resolve_validation_failure(
    project: Project,
    store: StateStore,
    orchestrator: OrchestratorAgent,
    workspace: Workspace,
    *,
    max_task_attempts: int = 2,
    now: datetime | None = None,
    execution: TaskExecutionResult | None = None,
    services: InferenceServiceManager | None = None,
) -> OrchestrationStep:
    """VALIDATING → RETRYING or ESCALATING. Attempt counts are not model-owned."""
    timestamp = now or datetime.now(UTC)
    if project.state != ProjectState.VALIDATING:
        raise OrchestrationError(
            f"Project {project.project_id} is {project.state}, not VALIDATING"
        )
    task_id = project.current_task_id
    if not task_id:
        raise OrchestrationError("VALIDATING project is missing current_task_id")
    task = await store.tasks.get(project.project_id, task_id)
    if task is None:
        raise OrchestrationError(f"Task not found: {project.project_id}/{task_id}")

    target = ProjectStateMachine.next_after_validation_failure(
        task.attempt_count, max_task_attempts
    )
    required = (
        OrchestratorDecision.RETRY_WITH_FIX
        if target is ProjectState.RETRYING
        else OrchestratorDecision.ESCALATE_TO_PLANNER
    )
    reduced = await _reduced_error(workspace, store, project.project_id, task, execution)
    decision = await _ask_orchestrator(
        orchestrator,
        project,
        task,
        required,
        max_task_attempts,
        reduced,
        services=services,
    )
    if target is ProjectState.RETRYING:
        return await _retry(project, task, store, decision, timestamp)
    return await _escalate(
        project, task, store, workspace, decision, reduced, timestamp
    )


async def _ask_orchestrator(
    orchestrator: OrchestratorAgent,
    project: Project,
    task: Task,
    required: OrchestratorDecision,
    max_task_attempts: int,
    reduced: ReducedLog | None,
    *,
    services: InferenceServiceManager | None = None,
) -> OrchestratorResult:
    try:
        if services is not None:
            await services.ensure_role_available(AgentRole.ORCHESTRATOR)
        return await orchestrator.decide(
            project_name=project.name,
            objective=project.objective,
            task=task,
            required_decision=required,
            attempt_count=task.attempt_count,
            max_task_attempts=max_task_attempts,
            reduced_error=reduced.render() if reduced is not None else (project.last_error or ""),
        )
    except _ORCHESTRATOR_FAILURES as exc:
        target = (
            TargetAgent.WORKER
            if required is OrchestratorDecision.RETRY_WITH_FIX
            else TargetAgent.PLANNER
        )
        instruction = (
            project.last_error
            or str(exc)
            or "Retry the assigned task using the reduced error context."
        )
        return OrchestratorResult(
            decision_summary=f"Harness fallback after orchestrator error: {exc}",
            decision=required,
            task_id=task.task_id,
            next_payload=NextPayload(
                target_agent=target,
                instruction=instruction,
            ),
        )


async def _retry(
    project: Project,
    task: Task,
    store: StateStore,
    decision: OrchestratorResult,
    timestamp: datetime,
) -> OrchestrationStep:
    instruction = (
        decision.next_payload.instruction if decision.next_payload is not None else ""
    )
    required_files = (
        list(decision.next_payload.required_files) if decision.next_payload else []
    )
    context_files = list(task.required_context)
    for path in required_files:
        if path not in context_files:
            context_files.append(path)
    task = task.model_copy(
        update={"status": TaskStatus.READY, "required_context": context_files}
    )
    await store.tasks.save(project.project_id, task)
    project = ProjectStateMachine.transition(
        project.model_copy(update={"current_task_id": task.task_id}),
        ProjectState.RETRYING,
        now=timestamp,
    )
    await store.projects.save(project)
    await _checkpoint(
        store,
        project,
        CheckpointKind.RETRY,
        timestamp,
        f"{project.project_id}-{task.task_id}-retry-{task.attempt_count}",
        {
            "task_id": task.task_id,
            "attempt": task.attempt_count,
            "instruction": instruction,
            "required_files": required_files,
            "decision_summary": decision.decision_summary,
        },
    )
    await _record_event(
        store,
        project,
        task.task_id,
        task.attempt_count,
        "retry_scheduled",
        timestamp,
        {"instruction": instruction},
    )
    return OrchestrationStep(
        project=project,
        decision=OrchestratorDecision.RETRY_WITH_FIX,
        orchestrator_result=decision,
    )


async def _escalate(
    project: Project,
    task: Task,
    store: StateStore,
    workspace: Workspace,
    decision: OrchestratorResult,
    reduced: ReducedLog | None,
    timestamp: datetime,
) -> OrchestrationStep:
    graph = TaskGraph(await store.tasks.list_for_project(project.project_id))
    graph.set_status(task.task_id, TaskStatus.FAILED)
    await store.tasks.save_all(project.project_id, graph.tasks())
    task = graph.get(task.task_id)
    attempts = await store.attempts.list_for_task(project.project_id, task.task_id)
    logs = [
        item
        for item in await _reduced_from_attempts(workspace, attempts)
        if item is not None
    ]
    if reduced is not None and not logs:
        logs = [reduced]
    package = EscalationPackage(
        failed_task=task,
        attempts=attempts,
        relevant_test_results=logs,
        affected_dependencies=[
            item.task_id for item in graph.tasks() if task.task_id in item.dependencies
        ],
        affected_files=_affected_files(task, attempts),
        last_error=project.last_error,
        decision_summary=decision.decision_summary,
    )
    project = ProjectStateMachine.transition(
        project.model_copy(update={"current_task_id": task.task_id}),
        ProjectState.ESCALATING,
        now=timestamp,
    )
    await store.projects.save(project)
    await _checkpoint(
        store,
        project,
        CheckpointKind.ESCALATION,
        timestamp,
        f"{project.project_id}-{task.task_id}-escalation-{task.attempt_count}",
        package.model_dump(mode="json"),
    )
    await _record_event(
        store,
        project,
        task.task_id,
        task.attempt_count,
        "escalated",
        timestamp,
        {
            "affected_dependencies": package.affected_dependencies,
            "decision_summary": decision.decision_summary,
        },
    )
    return OrchestrationStep(
        project=project,
        decision=OrchestratorDecision.ESCALATE_TO_PLANNER,
        orchestrator_result=decision,
        escalation=package,
    )


async def _complete(
    project: Project, store: StateStore, timestamp: datetime
) -> OrchestrationStep:
    project = ProjectStateMachine.transition(
        project.model_copy(update={"last_error": None, "current_task_id": None}),
        ProjectState.COMPLETED,
        now=timestamp,
    )
    await store.projects.save(project)
    await _checkpoint(
        store,
        project,
        CheckpointKind.TASK_COMPLETION,
        timestamp,
        f"{project.project_id}-mission-complete",
        {"decision": OrchestratorDecision.MISSION_COMPLETE.value},
    )
    await _record_event(
        store, project, None, None, "mission_complete", timestamp, {}
    )
    return OrchestrationStep(
        project=project, decision=OrchestratorDecision.MISSION_COMPLETE
    )


async def _blocked(
    project: Project,
    store: StateStore,
    graph: TaskGraph,
    timestamp: datetime,
) -> OrchestrationStep:
    blocked = [task.task_id for task in graph.blocked_tasks()]
    pending = [
        task.task_id
        for task in graph.tasks()
        if task.status in {TaskStatus.PENDING, TaskStatus.TESTING, TaskStatus.FAILED}
    ]
    message = "No eligible tasks"
    if blocked:
        message += "; blocked: " + ", ".join(blocked)
    elif pending:
        message += "; remaining: " + ", ".join(pending)
    project = project.model_copy(update={"last_error": message, "updated_at": timestamp})
    await store.projects.save(project)
    await _record_event(store, project, None, None, "blocked", timestamp, {"error": message})
    return OrchestrationStep(project=project, decision=OrchestratorDecision.BLOCKED)


async def _reduced_error(
    workspace: Workspace,
    store: StateStore,
    project_id: str,
    task: Task,
    execution: TaskExecutionResult | None,
) -> ReducedLog | None:
    reducer = LogReducer()
    if execution is not None and execution.test_results:
        return reducer.reduce(execution.test_results[-1])
    attempts = await store.attempts.list_for_task(project_id, task.task_id)
    logs = await _reduced_from_attempts(workspace, attempts)
    for item in reversed(logs):
        if item is not None:
            return item
    return None


async def _reduced_from_attempts(
    workspace: Workspace, attempts: list[TaskAttempt]
) -> list[ReducedLog | None]:
    reducer = LogReducer()
    files = WorkspaceFilesystem(workspace)
    results: list[ReducedLog | None] = []
    for attempt in attempts:
        if attempt.result not in _FAILED_ATTEMPTS or not attempt.test_log_path:
            results.append(None)
            continue
        try:
            text = await files.read_file(attempt.test_log_path)
        except WorkspaceError:
            results.append(None)
            continue
        results.append(reducer.reduce_log_text(text))
    return results


def _affected_files(task: Task, attempts: list[TaskAttempt]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for path in (*task.target_files, *task.test_files, *task.required_context):
        if path not in seen:
            seen.add(path)
            paths.append(path)
    for attempt in attempts:
        response = attempt.worker_response or {}
        claimed = response.get("changed_files")
        if not isinstance(claimed, list):
            continue
        for path in claimed:
            if isinstance(path, str) and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


async def _checkpoint(
    store: StateStore,
    project: Project,
    kind: CheckpointKind,
    timestamp: datetime,
    checkpoint_id: str,
    payload: dict[str, object],
) -> None:
    await store.checkpoints.save(
        Checkpoint(
            checkpoint_id=checkpoint_id,
            project_id=project.project_id,
            kind=kind,
            created_at=timestamp,
            payload=payload,
        )
    )


async def _record_event(
    store: StateStore,
    project: Project,
    task_id: str | None,
    attempt: int | None,
    event: str,
    timestamp: datetime,
    payload: dict[str, object],
) -> None:
    await store.events.append(
        EventRecord(
            timestamp=timestamp,
            project_id=project.project_id,
            task_id=task_id,
            component="orchestrator",
            event=event,
            attempt=attempt,
            payload=payload,
        )
    )

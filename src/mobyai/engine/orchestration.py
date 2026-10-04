"""Orchestrator loop: select, dispatch, evaluate, retry, escalate, complete.

Counters, DAG eligibility, and transitions are application-owned. The Orchestrator
model only writes a remediation or escalation summary.
"""

from __future__ import annotations

from mobyai.records.capture import operation

from mobyai.tracing import traced

from datetime import UTC, datetime
import json
import asyncio
from itertools import count

from mobyai.agents.orchestrator import OrchestratorAgent, OrchestratorValidationError
from mobyai.agents.planner import PlannerAgent
from mobyai.agents.worker import WorkerAgent
from mobyai.context.log_reducer import LogReducer, ReducedLog
from mobyai.dag.graph import TaskGraph
from mobyai.engine.state_machine import ProjectStateMachine
from mobyai.engine.replanning import replan_project, ReplanError
from mobyai.inference.recovery import recovery_stage, MAX_RECOVERY_STAGES
from mobyai.execution.diagnostics import diagnose
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
        self.defer_sampling = False


@traced("orchestrator.step")
@operation("orchestrator", "run")
async def advance_project(
    project: Project,
    store: StateStore,
    worker: WorkerAgent,
    orchestrator: OrchestratorAgent,
    workspace: Workspace,
    *,
    max_task_attempts: int = 2,
    token_budget: int = 48000,
    memory_token_budget: int = 2000,
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
            memory_token_budget=memory_token_budget,
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
        memory_token_budget=memory_token_budget,
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
        memory_token_budget=memory_token_budget,
    )


@operation("harness", "run")
async def run_project(
    project: Project,
    store: StateStore,
    worker: WorkerAgent,
    orchestrator: OrchestratorAgent,
    workspace: Workspace,
    *,
    max_task_attempts: int = 2,
    max_steps: int | None = None,
    token_budget: int = 48000,
    memory_token_budget: int = 2000,
    max_turns: int = 8,
    command_timeout_seconds: int = 600,
    now: datetime | None = None,
    planner: PlannerAgent | None = None,
    max_replans: int | None = None,
    services: InferenceServiceManager | None = None,
    prefer_import_contracts: bool = True,
) -> OrchestrationStep:
    """Recover until complete or blocked. Legacy max_replans is ignored.

    Explicit max_steps requests an operator pause, not a project budget failure.
    """
    timestamp = now or datetime.now(UTC)
    current = project
    last: OrchestrationStep | None = None
    planner_round = 0
    checkpoints = await store.checkpoints.list(project.project_id)
    replans = sum(item.kind is CheckpointKind.REPLAN for item in checkpoints)
    history = await store.events.list_for_project(project.project_id)
    replans = max(replans, sum(item.event == "dag_replanned" for item in history))
    stage = next((int(item.payload["stage"]) for item in reversed(history)
                  if item.event == "sampling_recovery"), 0)
    for _ in (count() if max_steps is None else range(max(max_steps, 1))):
        await asyncio.sleep(0)
        timestamp = now or datetime.now(UTC)
        from mobyai.review.service import requested_report
        from types import SimpleNamespace
        if planner is not None:
            await requested_report(SimpleNamespace(store=store,planner=planner,services=services),current)
        if current.state == ProjectState.ESCALATING:
            if planner is None:
                if last is not None:
                    return last
                raise OrchestrationError(
                    f"Project {project.project_id} is ESCALATING without a Planner"
                )
            try:
                with recovery_stage(stage):
                    result = await replan_project(
                        current,
                        store,
                        planner,
                        workspace=workspace,
                        now=timestamp,
                        services=services,
                    )
            except ReplanError as exc:
                from mobyai.engine.planner_recovery import recover_planner
                planner_round += 1
                current = await recover_planner(current,store,exc,ProjectState.ESCALATING,planner_round)
                if current.state == ProjectState.PAUSED:
                    return OrchestrationStep(project=current,decision=OrchestratorDecision.BLOCKED)
                continue
            current = result.project
            replans += 1
            continue
        with recovery_stage(stage):
            last = await advance_project(
                current,
                store,
                worker,
                orchestrator,
                workspace,
                max_task_attempts=max_task_attempts,
                token_budget=token_budget,
                memory_token_budget=memory_token_budget,
                max_turns=max_turns,
                command_timeout_seconds=command_timeout_seconds,
                now=timestamp,
                services=services,
                prefer_import_contracts=prefer_import_contracts,
            )
        current = last.project
        if last.execution is not None:
            results = getattr(last.execution, "test_results", [])
            evidence = "\n".join(r.stdout + "\n" + r.stderr for r in results if r.exit_code != 0)
            error_diagnosis = diagnose(current.last_error or "")
            diagnosis = (error_diagnosis if error_diagnosis.category in {"timeout", "timeout_approval", "context_capacity"}
                         else diagnose(evidence or current.last_error or ""))
            next_stage = stage
            if last.execution.passed:
                next_stage = 0
            elif replans >= 1 and diagnosis.sampling_eligible and last.decision is not OrchestratorDecision.BLOCKED and not getattr(last,"defer_sampling",False):
                next_stage = min(stage + 1, MAX_RECOVERY_STAGES)
            if next_stage != stage:
                stage = next_stage
                await _record_event(store, current, current.current_task_id, None,
                    "sampling_recovery", datetime.now(UTC),
                    {"stage": stage, "category": diagnosis.category,
                     "reason": "failed_after_replan" if stage else "task_passed"})
        if current.state in {ProjectState.COMPLETED, ProjectState.FAILED}:
            return last
        if last.decision is OrchestratorDecision.BLOCKED:
            return last
        if current.state == ProjectState.ESCALATING and planner is None:
            return last
    from mobyai.engine.stopping import pause_interrupted
    paused = await pause_interrupted(store, project.project_id)
    await _record_event(store, paused, paused.current_task_id, None,
                        'operator_step_limit_reached', datetime.now(UTC), {'limit':max_steps})
    return OrchestrationStep(project=paused,decision=OrchestratorDecision.BLOCKED)


@operation("orchestrator", "run")
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
    memory_token_budget: int = 2000,
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
    history = await store.events.list_for_project(project.project_id)
    offered = next((e for e in reversed(history) if e.task_id == task.task_id and e.event == 'cached_repair_offered'),None)
    evidence = reduced.render() if reduced else project.last_error or ''
    if diagnose(project.last_error or '').category in {'context_capacity', 'timeout', 'timeout_approval'}:
        evidence = project.last_error
    candidates = []
    diagnosis = diagnose(evidence)
    if diagnosis.category in {"command_syntax", "service_lifecycle"}:
        # Workers cannot edit the saved task contract. Route this to its owner
        # immediately, even on the first failure, instead of retrying setup.
        target = ProjectState.ESCALATING
        required = OrchestratorDecision.ESCALATE_TO_PLANNER
    if task.attempt_count >= 2 and diagnosis.sampling_eligible and offered is None:
        from mobyai.research.client import research_lookup
        found = await research_lookup({'query':evidence[:12000] or task.description})
        candidates = found.get('results',[]) if found.get('status') == 'ok' else []
        await _record_event(store,project,task.task_id,task.attempt_count,'cache_recovery_lookup',timestamp,{'status':found.get('status'),'references':candidates})
        if candidates:
            required = OrchestratorDecision.RETRY_WITH_FIX
    used_urls = []
    if offered and task.attempt_count > (offered.attempt or 0):
        attempts = await store.attempts.list_for_task(project.project_id,task.task_id)
        used_urls = getattr(getattr(execution,'worker_result',None),'reference_urls_used',None)
        if used_urls is None:
            used_urls = (attempts[-1].worker_response or {}).get('reference_urls_used',[]) if attempts else []
        await _record_event(store,project,task.task_id,task.attempt_count,'cached_repair_failed',timestamp,
                            {'references_supplied':offered.payload.get('references',[]),
                             'worker_reported_urls':used_urls})
    from mobyai.engine.recovery_evidence import evidence_for, fingerprint, record_decision
    from mobyai.memory.assembly import working_set_for
    recovery_evidence = await evidence_for(store, project.project_id, task, workspace, evidence)
    # Memory is keyed by the persisted harness error, not its rendered log.
    memory_signature = fingerprint(project.last_error or "", task.test_commands)
    shared_memory = await working_set_for(
        store,
        project.project_id,
        task.task_id,
        failure_signature=memory_signature,
        token_budget=memory_token_budget,
    )
    if diagnosis.category in {"command_syntax", "service_lifecycle"}:
        decision = OrchestratorResult(
            decision_summary="Saved validation requires command contract repair by the Planner.",
            decision=required,
            task_id=task.task_id,
            next_payload=NextPayload(target_agent=TargetAgent.PLANNER,
                                     instruction=diagnosis.instruction),
        )
    else:
        decision = await _ask_orchestrator(
            orchestrator,
            project,
            task,
            required,
            max_task_attempts,
            reduced,
            services=services,
            cache_evidence=candidates,
            recovery_evidence=recovery_evidence,
            reference_use=({"supplied":offered.payload.get("references",[]),"reported_used":used_urls} if offered else None),
            shared_memory=shared_memory,
        )
    if task.attempt_count >= 2 and diagnosis.sampling_eligible:
        if candidates and decision.cached_references_sufficient and decision.next_payload is not None:
            target = ProjectState.RETRYING
            instruction = decision.next_payload.instruction + "\nCached references (verify before using): " + json.dumps(candidates) + "\nRepeat unchanged validation commands: " + json.dumps(task.test_commands) + "\nReport reference_urls_used in WorkerResult; do not claim use if only supplied."
            decision = decision.model_copy(update={"next_payload":decision.next_payload.model_copy(update={"instruction":instruction})})
            await _record_event(store,project,task.task_id,task.attempt_count,"cached_repair_offered",timestamp,{"references":candidates})
        else:
            target = ProjectState.ESCALATING
            decision = decision.model_copy(update={"decision":OrchestratorDecision.ESCALATE_TO_PLANNER, "next_payload":NextPayload(target_agent=TargetAgent.PLANNER,instruction="[RESEARCH_REQUIRED] Review failure logs and prior attempts, then use internet research for the missing evidence."), "decision_summary":decision.decision_summary + "\n[RESEARCH_REQUIRED] Review logs and prior attempts before internet research. Cache recovery: " + ("worker repair failed; worker reported URLs: " + json.dumps(used_urls) if offered else "no applicable cached evidence")})
    diagnosis = diagnose(evidence)
    if decision.next_payload is not None:
        payload = decision.next_payload.model_copy(update={
            "instruction": decision.next_payload.instruction + "\n" + diagnosis.instruction,
            "required_files": list(dict.fromkeys([*decision.next_payload.required_files,
                *(["package.json"] if diagnosis.category.startswith("npm_") and (workspace.root / "package.json").is_file() else [])])),
        })
        decision = decision.model_copy(update={"next_payload": payload,
            "decision_summary": decision.decision_summary + "\n" + diagnosis.instruction})
    decision = await record_decision(store, project.project_id, task, decision, recovery_evidence)
    await _record_event(store, project, task.task_id, task.attempt_count,
        "failure_diagnosed", timestamp, {"category": diagnosis.category, "instruction": diagnosis.instruction})
    if not diagnosis.sampling_eligible and not diagnosis.locally_repairable:
        from mobyai.engine.stopping import pause_interrupted
        paused = await pause_interrupted(store, project.project_id)
        return OrchestrationStep(project=paused, decision=OrchestratorDecision.BLOCKED, execution=execution)
    if target is ProjectState.RETRYING:
        result = await _retry(
            project, task, store, decision, timestamp, memory_token_budget=memory_token_budget
        )
        result.execution = execution
        result.defer_sampling = task.attempt_count >= 2
        return result
    result = await _escalate(
        project,
        task,
        store,
        workspace,
        decision,
        reduced,
        timestamp,
        failure_signature=memory_signature,
        memory_token_budget=memory_token_budget,
    )
    result.execution = execution
    result.defer_sampling = task.attempt_count >= 2
    return result


@traced("orchestrator.decide")
async def _ask_orchestrator(
    orchestrator: OrchestratorAgent,
    project: Project,
    task: Task,
    required: OrchestratorDecision,
    max_task_attempts: int,
    reduced: ReducedLog | None,
    *,
    services: InferenceServiceManager | None = None,
    cache_evidence: list | None = None,
    recovery_evidence: dict | None = None,
    reference_use: dict | None = None,
    shared_memory: str = "",
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
            reduced_error=(project.last_error or "") + ("\nHarness validation:\n" + reduced.render() if reduced is not None else "") + ("\nRECOVERY EVIDENCE: " + json.dumps(recovery_evidence) if recovery_evidence else "") + ("\n" + shared_memory if shared_memory else "") + ("\nUNTRUSTED LOCAL CACHE CANDIDATES: " + json.dumps(cache_evidence) if cache_evidence else "") + ("\nFAILED REFERENCE-ASSISTED ATTEMPT: " + json.dumps(reference_use) if reference_use else ""),
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
    *,
    memory_token_budget: int = 2000,
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
    from mobyai.memory.record import record_retry_memory
    shared_memory = await record_retry_memory(
        store,
        project_id=project.project_id,
        task_id=task.task_id,
        instruction=instruction,
        now=timestamp,
        token_budget=memory_token_budget,
    )
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
            "shared_memory": shared_memory,
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
    *,
    failure_signature: str | None = None,
    memory_token_budget: int = 2000,
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
    from mobyai.memory.record import record_escalation_memory
    instruction = decision.next_payload.instruction if decision.next_payload is not None else ""
    shared_memory = await record_escalation_memory(
        store,
        project_id=project.project_id,
        task_id=task.task_id,
        summary=decision.decision_summary,
        instruction=instruction,
        now=timestamp,
        failure_signature=failure_signature,
        token_budget=memory_token_budget,
    )
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
        shared_memory=shared_memory,
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

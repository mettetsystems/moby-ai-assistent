"""Single-task Worker loop. The harness owns files, tests, attempts, and transitions."""

from __future__ import annotations

from mobyai.tracing import traced

from collections.abc import Sequence
from datetime import UTC, datetime

from mobyai.agents.prompts.worker import TOOL_FEEDBACK_PROMPT
from mobyai.agents.worker import WorkerAgent
from mobyai.context.contracts import record_module_contracts
from mobyai.context.packer import ContextPacker
from mobyai.context.errors import ContextBudgetError
from mobyai.dag.graph import TaskGraph
from mobyai.engine.state_machine import ProjectStateMachine
from mobyai.execution.commands import CommandResult, CommandRunner
from mobyai.execution.errors import WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.logs import AttemptLogStore
from mobyai.execution.tests import TestRunner, passed as test_passed
from mobyai.execution.tools import ToolDispatcher, ToolOutcome, format_tool_outcomes, split_command
from mobyai.execution.workspace import Workspace
from mobyai.inference.base import InferenceError, SchemaRepairExhaustedError
from mobyai.inference.service_manager import InferenceServiceManager
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.config import AgentRole
from mobyai.models.event import EventRecord
from mobyai.models.inference import ChatMessage
from mobyai.models.project import Project, ProjectState
from mobyai.models.task import Task, TaskStatus
from mobyai.models.worker import WorkerResult
from mobyai.state.repository import StateStore

_START_STATES = frozenset(
    {
        ProjectState.READY,
        ProjectState.DISPATCHING,
        ProjectState.RETRYING,
        ProjectState.WORKING,
    }
)
_WORKER_FAILURES = (SchemaRepairExhaustedError, InferenceError)
_ELIGIBLE_TASKS = frozenset({TaskStatus.READY, TaskStatus.RUNNING})


class WorkingError(Exception):
    """The Worker loop could not start or the task is not eligible."""


class TaskExecutionResult:
    def __init__(
        self,
        *,
        project: Project,
        task: Task,
        attempt: TaskAttempt,
        passed: bool,
        changed_files: list[str],
        missing_files: list[str],
        worker_result: WorkerResult | None,
        test_results: list[CommandResult],
        tool_outcomes: list[ToolOutcome],
    ) -> None:
        self.project = project
        self.task = task
        self.attempt = attempt
        self.passed = passed
        self.changed_files = changed_files
        self.missing_files = missing_files
        self.worker_result = worker_result
        self.test_results = test_results
        self.tool_outcomes = tool_outcomes


@traced("worker.attempt")
async def execute_task(
    project: Project,
    task_id: str,
    store: StateStore,
    agent: WorkerAgent,
    workspace: Workspace,
    *,
    token_budget: int = 48000,
    max_turns: int = 8,
    command_timeout_seconds: int = 600,
    max_output_bytes: int = 1_048_576,
    max_log_bytes: int = 52_428_800,
    extra_instruction: str | None = None,
    now: datetime | None = None,
    services: InferenceServiceManager | None = None,
    prefer_import_contracts: bool = True,
) -> TaskExecutionResult:
    """Run one Worker attempt: tools → filesystem → harness tests.

    On pass: VALIDATING → READY and the task is PASSED.
    On fail: project stays VALIDATING. Retry vs escalate is Phase 8.
    """
    from mobyai.review.service import ensure_approved
    await ensure_approved(store, project)
    timestamp = now or datetime.now(UTC)
    if project.state not in _START_STATES:
        raise WorkingError(
            f"Project {project.project_id} is {project.state}, not awaiting Worker execution"
        )
    task = await store.tasks.get(project.project_id, task_id)
    if task is None:
        raise WorkingError(f"Task not found: {project.project_id}/{task_id}")
    if task.status not in _ELIGIBLE_TASKS:
        raise WorkingError(
            f"Task {task_id} is {task.status}, not eligible for Worker execution"
        )

    filesystem = WorkspaceFilesystem(workspace)
    commands = CommandRunner(workspace, max_output_bytes=max_output_bytes)
    tests = TestRunner(commands, workspace)
    logs = AttemptLogStore(workspace, max_log_bytes=max_log_bytes)
    dispatcher = ToolDispatcher(
        filesystem,
        workspace,
        commands,
        tests,
        timeout_seconds=command_timeout_seconds,
    )
    packer = ContextPacker(
        filesystem,
        store,
        prefer_import_contracts=prefer_import_contracts,
    )

    project, task, attempt = await _enter_working(
        project, task, store, timestamp
    )
    extra_messages: list[ChatMessage] = []
    worker_result: WorkerResult | None = None
    outcomes: list[ToolOutcome] = []
    changed_files: list[str] = []
    references_used: list[str] = []
    inference_error: str | None = None

    try:
        if services is not None:
            await services.ensure_role_available(AgentRole.WORKER)
        remediation = extra_instruction or await _remediation_from_store(
            store, project.project_id, task.task_id
        )
        context = await packer.build_worker_context(
            project.project_id,
            task.task_id,
            token_budget,
            remediation_instruction=remediation,
        )
        for _turn in range(max(max_turns, 1)):
            try:
                worker_result = await agent.implement(
                    context, extra_messages=extra_messages
                )
            except _WORKER_FAILURES as exc:
                inference_error = str(exc)
                break
            _extend_unique(references_used, worker_result.reference_urls_used)
            worker_result = worker_result.model_copy(update={"reference_urls_used":references_used[:]})
            turn_outcomes = await dispatcher.execute_all(worker_result.tool_calls)
            for outcome in turn_outcomes:
                await _record_event(store, project, task.task_id, attempt.attempt_number,
                    "tool_finished", now or datetime.now(UTC), {"turn": _turn + 1,
                    "tool": outcome.name.value, "ok": outcome.ok,
                    "exit_code": outcome.exit_code, "error": outcome.error,
                    "output_tail": outcome.output[-3000:] if not outcome.ok or outcome.name.value in {"research_search","research_lookup"} else ""})
            outcomes.extend(turn_outcomes)
            _extend_unique(changed_files, _changed_from(turn_outcomes))
            if worker_result.done or not worker_result.tool_calls:
                break
            if getattr(agent, "refresh_context_enabled", False):
                context = await packer.build_worker_context(project.project_id, task.task_id,
                    token_budget, remediation_instruction=remediation)
            extra_messages = [
                *extra_messages,
                ChatMessage(role="assistant", content=worker_result.model_dump_json()),
                ChatMessage(
                    role="user",
                    content=TOOL_FEEDBACK_PROMPT.format(
                        results=format_tool_outcomes(turn_outcomes)
                    ),
                ),
            ]
            if getattr(agent, "compact_history_enabled", False):
                from mobyai.context.history import compact_history
                extra_messages = compact_history(extra_messages)
    except (WorkspaceError, ContextBudgetError) as exc:
        inference_error = inference_error or (("Context budget exhausted: " if isinstance(exc,ContextBudgetError) else "") + str(exc))

    task = task.model_copy(update={"status": TaskStatus.TESTING})
    await store.tasks.save(project.project_id, task)
    project = ProjectStateMachine.transition(
        project.model_copy(update={"last_error": None}),
        ProjectState.VALIDATING,
        now=timestamp,
    )
    await store.projects.save(project)

    test_results, log_path = await _run_harness_tests(
        task,
        tests,
        logs,
        timeout_seconds=command_timeout_seconds,
    )
    missing_files = _missing_required_files(workspace, task)
    tests_ok = bool(test_results) and all(test_passed(item) for item in test_results)
    timed_out = any(item.timed_out for item in test_results)
    if inference_error:
        attempt_result = AttemptResult.ERROR
        last_error = inference_error
        succeeded = False
    elif not task.test_commands:
        attempt_result = AttemptResult.FAIL
        last_error = f"{task.task_id} has no test_commands"
        succeeded = False
    elif timed_out:
        attempt_result = AttemptResult.TIMEOUT
        last_error = "Harness tests timed out"
        succeeded = False
    elif not tests_ok or missing_files:
        attempt_result = AttemptResult.FAIL
        last_error = _failure_message(test_results, missing_files)
        succeeded = False
    else:
        attempt_result = AttemptResult.PASS
        last_error = None
        succeeded = True

    finished = TaskAttempt(
        id=attempt.id,
        project_id=attempt.project_id,
        task_id=attempt.task_id,
        attempt_number=attempt.attempt_number,
        started_at=attempt.started_at,
        finished_at=timestamp,
        worker_response=worker_result.model_dump(mode="json") if worker_result else None,
        test_exit_code=_exit_code(test_results),
        test_log_path=log_path,
        result=attempt_result,
    )
    finished = await store.attempts.save(finished)
    await _checkpoint(
        store,
        project,
        CheckpointKind.WORKER_EXECUTION,
        timestamp,
        f"{project.project_id}-{task.task_id}-worker-{attempt.attempt_number}",
        {
            "task_id": task.task_id,
            "attempt": attempt.attempt_number,
            "changed_files": changed_files,
            "done": bool(worker_result.done) if worker_result else False,
        },
    )
    await _checkpoint(
        store,
        project,
        CheckpointKind.VALIDATION,
        timestamp,
        f"{project.project_id}-{task.task_id}-validation-{attempt.attempt_number}",
        {
            "task_id": task.task_id,
            "attempt": attempt.attempt_number,
            "result": attempt_result.value,
            "missing_files": missing_files,
        },
    )

    if succeeded:
        graph = TaskGraph(await store.tasks.list_for_project(project.project_id))
        graph.set_status(task.task_id, TaskStatus.PASSED)
        await store.tasks.save_all(project.project_id, graph.tasks())
        task = graph.get(task.task_id)
        project = ProjectStateMachine.transition(
            project.model_copy(
                update={"last_error": None, "current_task_id": None}
            ),
            ProjectState.READY,
            now=timestamp,
        )
        await store.projects.save(project)
        await _checkpoint(
            store,
            project,
            CheckpointKind.TASK_COMPLETION,
            timestamp,
            f"{project.project_id}-{task.task_id}-complete-{attempt.attempt_number}",
            {"task_id": task.task_id, "changed_files": changed_files},
        )
        await record_module_contracts(
            store,
            filesystem,
            project_id=project.project_id,
            task_id=task.task_id,
            paths=[*task.target_files, *changed_files],
            now=timestamp,
        )
        await _record_event(
            store,
            project,
            task.task_id,
            attempt.attempt_number,
            "task_passed",
            timestamp,
            {"changed_files": changed_files},
        )
    else:
        await store.tasks.save(project.project_id, task)
        project = project.model_copy(update={"last_error": last_error, "updated_at": timestamp})
        await store.projects.save(project)
        await _record_event(
            store,
            project,
            task.task_id,
            attempt.attempt_number,
            "task_failed",
            timestamp,
            {"result": attempt_result.value, "error": last_error},
        )

    return TaskExecutionResult(
        project=project,
        task=task,
        attempt=finished,
        passed=succeeded,
        changed_files=changed_files,
        missing_files=missing_files,
        worker_result=worker_result,
        test_results=test_results,
        tool_outcomes=outcomes,
    )


async def _enter_working(
    project: Project,
    task: Task,
    store: StateStore,
    timestamp: datetime,
) -> tuple[Project, Task, TaskAttempt]:
    reusing = False
    attempt_number = task.attempt_count
    if project.state == ProjectState.WORKING:
        existing = await store.attempts.list_for_task(project.project_id, task.task_id)
        in_progress = [
            item for item in existing if item.result == AttemptResult.IN_PROGRESS
        ]
        if in_progress:
            reusing = True
            attempt_number = in_progress[-1].attempt_number

    dispatched_now = False
    if project.state == ProjectState.RETRYING:
        project = ProjectStateMachine.transition(
            project.model_copy(update={"current_task_id": task.task_id}),
            ProjectState.DISPATCHING,
            now=timestamp,
        )
        await store.projects.save(project)
        dispatched_now = True
    if project.state == ProjectState.READY:
        project = ProjectStateMachine.transition(
            project.model_copy(update={"current_task_id": task.task_id}),
            ProjectState.DISPATCHING,
            now=timestamp,
        )
        await store.projects.save(project)
        dispatched_now = True
    if dispatched_now:
        await _checkpoint(
            store,
            project,
            CheckpointKind.TASK_DISPATCH,
            timestamp,
            f"{project.project_id}-{task.task_id}-dispatch-{attempt_number + 1}",
            {"task_id": task.task_id},
        )

    if project.state == ProjectState.DISPATCHING:
        project = ProjectStateMachine.transition(
            project.model_copy(update={"current_task_id": task.task_id}),
            ProjectState.WORKING,
            now=timestamp,
        )
        await store.projects.save(project)

    if not reusing:
        attempt_number = task.attempt_count + 1
        task = task.model_copy(
            update={"attempt_count": attempt_number, "status": TaskStatus.RUNNING}
        )
        await store.tasks.save(project.project_id, task)
        attempt = await store.attempts.save(
            TaskAttempt(
                project_id=project.project_id,
                task_id=task.task_id,
                attempt_number=attempt_number,
                started_at=timestamp,
                result=AttemptResult.IN_PROGRESS,
            )
        )
        await _record_event(
            store,
            project,
            task.task_id,
            attempt_number,
            "task_dispatched",
            timestamp,
            {"attempt": attempt_number},
        )
        return project, task, attempt

    task = task.model_copy(update={"status": TaskStatus.RUNNING})
    await store.tasks.save(project.project_id, task)
    existing = await store.attempts.get(project.project_id, task.task_id, attempt_number)
    if existing is None:
        raise WorkingError(f"Missing in-progress attempt for {task.task_id}")
    return project, task, existing


async def _run_harness_tests(
    task: Task,
    tests: TestRunner,
    logs: AttemptLogStore,
    *,
    timeout_seconds: int,
) -> tuple[list[CommandResult], str | None]:
    if not task.test_commands:
        return [], None
    argv_list = [split_command(command) for command in task.test_commands]
    results = await tests.run_all(argv_list, timeout_seconds=timeout_seconds)
    log_path: str | None = None
    if results:
        stored = await logs.persist(task.task_id, max(task.attempt_count, 1), results[-1])
        log_path = tests.workspace.relative(stored)
    return results, log_path


def _missing_required_files(workspace: Workspace, task: Task) -> list[str]:
    missing: list[str] = []
    seen: set[str] = set()
    for path in (*task.target_files, *task.test_files):
        if path in seen:
            continue
        seen.add(path)
        try:
            target = workspace.resolve(path, must_exist=True)
        except WorkspaceError:
            missing.append(path)
            continue
        if not target.is_file():
            missing.append(path)
    return missing


def _failure_message(results: Sequence[CommandResult], missing: Sequence[str]) -> str:
    parts: list[str] = []
    if missing:
        parts.append("Missing required files: " + ", ".join(missing))
    for result in results:
        if not test_passed(result):
            command = " ".join(result.command)
            parts.append(f"{command} exited {result.exit_code}")
    return "; ".join(parts) or "Harness validation failed"


def _exit_code(results: Sequence[CommandResult]) -> int | None:
    if not results:
        return None
    return results[-1].exit_code


def _changed_from(outcomes: Sequence[ToolOutcome]) -> list[str]:
    paths: list[str] = []
    for outcome in outcomes:
        if outcome.ok:
            _extend_unique(paths, outcome.changed_files)
    return paths


def _extend_unique(dest: list[str], paths: Sequence[str]) -> None:
    seen = set(dest)
    for path in paths:
        if path not in seen:
            seen.add(path)
            dest.append(path)


async def _remediation_from_store(
    store: StateStore, project_id: str, task_id: str
) -> str | None:
    checkpoints = await store.checkpoints.list(project_id)
    for item in reversed(checkpoints):
        if item.kind is not CheckpointKind.RETRY:
            continue
        if item.payload.get("task_id") != task_id:
            continue
        instruction = item.payload.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            return instruction
    return None


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
    task_id: str,
    attempt: int,
    event: str,
    timestamp: datetime,
    payload: dict[str, object],
) -> None:
    await store.events.append(
        EventRecord(
            timestamp=timestamp,
            project_id=project.project_id,
            task_id=task_id,
            component="worker",
            event=event,
            attempt=attempt,
            payload=payload,
        )
    )

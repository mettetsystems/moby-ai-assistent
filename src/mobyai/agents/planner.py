"""Planner agent: propose a Task DAG. It does not persist state or write app code."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mobyai.agents.prompts.planner import (
    DAG_REPAIR_PROMPT,
    INSPECT_DONE_PROMPT,
    INSPECT_FEEDBACK_PROMPT,
    PATCH_INSPECT_DONE_PROMPT,
    PATCH_INSPECT_FEEDBACK_PROMPT,
    PATCH_REPAIR_PROMPT,
    REPLAN_SYSTEM_PROMPT,
    REPLAN_USER_PROMPT,
    SYSTEM_PROMPT,
    USER_PROMPT,
)
from mobyai.dag.patch import DAGPatchError, affected_branch, apply_dag_patch
from mobyai.dag.validator import DAGValidationError, validate_dag
from mobyai.execution.tools import PLANNER_INSPECT_TOOLS, ToolDispatcher, format_tool_outcomes
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.escalation import EscalationPackage
from mobyai.models.inference import ChatMessage, ModelRequest
from mobyai.models.planner import (
    DAGPatch,
    PlannerInspectCall,
    PlannerOutput,
    PlannerPatchTurn,
    PlannerTaskSpec,
    PlannerTurn,
)
from mobyai.models.task import Task
from mobyai.models.worker import WorkerToolCall, WorkerToolName


class PlanValidationError(ValueError):
    """Pydantic succeeded but deterministic plan rules failed."""


class PlannerAgent:
    def __init__(
        self,
        generator: StructuredGenerator,
        *,
        max_plan_repairs: int | None = None,
        max_inspect_turns: int = 3,
    ) -> None:
        self._generator = generator
        self._max_plan_repairs = (
            generator.max_repair_attempts if max_plan_repairs is None else max(max_plan_repairs, 0)
        )
        self._max_inspect_turns = max(max_inspect_turns, 0)

    async def create_plan(
        self,
        objective: str,
        *,
        project_name: str,
        existing_paths: Sequence[str] = (),
        inspector: ToolDispatcher | None = None,
        module_contracts: Sequence[str] = (),
    ) -> PlannerOutput:
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=_render_user(
                    objective, project_name, existing_paths, module_contracts
                ),
            ),
        ]
        inspect_turns = 0
        plan_repairs = 0
        last_error: Exception | None = None
        while True:
            turn = await self._generator.generate(
                ModelRequest(messages=messages), PlannerTurn
            )
            if (
                turn.inspect_calls
                and inspector is not None
                and inspect_turns < self._max_inspect_turns
            ):
                outcomes = await inspector.execute_all(
                    _inspect_to_worker(turn.inspect_calls),
                    allowed=PLANNER_INSPECT_TOOLS,
                )
                inspect_turns += 1
                messages = [
                    *messages,
                    ChatMessage(role="assistant", content=turn.model_dump_json()),
                    ChatMessage(
                        role="user",
                        content=INSPECT_FEEDBACK_PROMPT.format(
                            results=format_tool_outcomes(outcomes),
                            remaining=self._max_inspect_turns - inspect_turns,
                        ),
                    ),
                ]
                continue
            if turn.plan is not None:
                try:
                    return validate_planner_output(turn.plan)
                except PlanValidationError as exc:
                    last_error = exc
                    if plan_repairs >= self._max_plan_repairs:
                        break
                    plan_repairs += 1
                    messages = [
                        *messages,
                        ChatMessage(role="assistant", content=turn.plan.model_dump_json()),
                        ChatMessage(
                            role="user",
                            content=DAG_REPAIR_PROMPT.format(errors=str(exc)),
                        ),
                    ]
                    continue
            last_error = PlanValidationError(
                _inspect_blocked_message(turn.inspect_calls, inspector, inspect_turns)
            )
            if plan_repairs >= self._max_plan_repairs:
                break
            plan_repairs += 1
            messages = [
                *messages,
                ChatMessage(role="assistant", content=turn.model_dump_json()),
                ChatMessage(role="user", content=INSPECT_DONE_PROMPT),
            ]
        raise PlanValidationError(
            f"Plan validation failed after {self._max_plan_repairs} repair(s): {last_error}"
        ) from last_error

    async def replan(
        self,
        *,
        objective: str,
        project_name: str,
        tasks: Sequence[Task],
        package: EscalationPackage,
        existing_paths: Sequence[str] = (),
        inspector: ToolDispatcher | None = None,
        module_contracts: Sequence[str] = (),
    ) -> DAGPatch:
        failed_id = package.failed_task.task_id
        messages = [
            ChatMessage(role="system", content=REPLAN_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=_render_replan(
                    objective=objective,
                    project_name=project_name,
                    tasks=tasks,
                    package=package,
                    existing_paths=existing_paths,
                    module_contracts=module_contracts,
                ),
            ),
        ]
        inspect_turns = 0
        plan_repairs = 0
        last_error: Exception | None = None
        while True:
            turn = await self._generator.generate(
                ModelRequest(messages=messages), PlannerPatchTurn
            )
            if (
                turn.inspect_calls
                and inspector is not None
                and inspect_turns < self._max_inspect_turns
            ):
                outcomes = await inspector.execute_all(
                    _inspect_to_worker(turn.inspect_calls),
                    allowed=PLANNER_INSPECT_TOOLS,
                )
                inspect_turns += 1
                messages = [
                    *messages,
                    ChatMessage(role="assistant", content=turn.model_dump_json()),
                    ChatMessage(
                        role="user",
                        content=PATCH_INSPECT_FEEDBACK_PROMPT.format(
                            results=format_tool_outcomes(outcomes),
                            remaining=self._max_inspect_turns - inspect_turns,
                        ),
                    ),
                ]
                continue
            if turn.patch is not None:
                try:
                    return validate_dag_patch(turn.patch, tasks, failed_task_id=failed_id)
                except PlanValidationError as exc:
                    last_error = exc
                    if plan_repairs >= self._max_plan_repairs:
                        break
                    plan_repairs += 1
                    messages = [
                        *messages,
                        ChatMessage(role="assistant", content=turn.patch.model_dump_json()),
                        ChatMessage(
                            role="user",
                            content=PATCH_REPAIR_PROMPT.format(errors=str(exc)),
                        ),
                    ]
                    continue
            last_error = PlanValidationError(
                _inspect_blocked_message(turn.inspect_calls, inspector, inspect_turns)
            )
            if plan_repairs >= self._max_plan_repairs:
                break
            plan_repairs += 1
            messages = [
                *messages,
                ChatMessage(role="assistant", content=turn.model_dump_json()),
                ChatMessage(role="user", content=PATCH_INSPECT_DONE_PROMPT),
            ]
        raise PlanValidationError(
            f"DAG patch validation failed after {self._max_plan_repairs} repair(s): {last_error}"
        ) from last_error


def validate_planner_output(plan: PlannerOutput) -> PlannerOutput:
    if not plan.tasks:
        raise PlanValidationError("Planner returned no tasks")
    for entry in plan.directory_structure:
        _assert_relative_path(entry, "directory_structure")
    for spec in plan.tasks:
        _validate_task_spec(spec)
    try:
        validate_dag([spec.to_task() for spec in plan.tasks])
    except DAGValidationError as exc:
        raise PlanValidationError(str(exc)) from exc
    return plan


def validate_dag_patch(
    patch: DAGPatch,
    tasks: Sequence[Task],
    *,
    failed_task_id: str,
) -> DAGPatch:
    for entry in patch.directory_structure:
        _assert_relative_path(entry, "directory_structure")
    for item in patch.supersede:
        for spec in item.replacements:
            _validate_task_spec(spec)
    for spec in patch.add_tasks:
        _validate_task_spec(spec)
    try:
        apply_dag_patch(tasks, patch, failed_task_id=failed_task_id)
    except DAGPatchError as exc:
        raise PlanValidationError(str(exc)) from exc
    return patch


def _validate_task_spec(spec: PlannerTaskSpec) -> None:
    if not spec.acceptance_criteria:
        raise PlanValidationError(f"{spec.task_id} is missing acceptance_criteria")
    if not spec.test_commands:
        raise PlanValidationError(f"{spec.task_id} is missing test_commands")
    for path in (*spec.target_files, *spec.test_files, *spec.required_context):
        _assert_relative_path(path, spec.task_id)


def _assert_relative_path(path: str, label: str) -> None:
    parsed = Path(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise PlanValidationError(f"{label} contains a non-relative path: {path}")


def _render_user(
    objective: str,
    project_name: str,
    existing_paths: Sequence[str],
    module_contracts: Sequence[str] = (),
) -> str:
    listing = "\n".join(existing_paths) if existing_paths else "(empty)"
    contracts = "\n\n".join(module_contracts) if module_contracts else "(none)"
    return USER_PROMPT.format(
        project_name=project_name,
        objective=objective.strip(),
        existing_paths=listing,
        module_contracts=contracts,
    )


def _render_replan(
    *,
    objective: str,
    project_name: str,
    tasks: Sequence[Task],
    package: EscalationPackage,
    existing_paths: Sequence[str],
    module_contracts: Sequence[str] = (),
) -> str:
    listing = "\n".join(existing_paths) if existing_paths else "(empty)"
    contracts = "\n\n".join(module_contracts) if module_contracts else "(none)"
    try:
        branch = sorted(affected_branch(tasks, package.failed_task.task_id))
    except DAGPatchError as exc:
        raise PlanValidationError(str(exc)) from exc
    dag_lines = [
        f"- {task.task_id} status={task.status.value} deps={list(task.dependencies)} name={task.name}"
        for task in tasks
    ]
    attempts = [
        {
            "attempt_number": attempt.attempt_number,
            "result": None if attempt.result is None else attempt.result.value,
            "test_exit_code": attempt.test_exit_code,
        }
        for attempt in package.attempts
    ]
    errors = [item.render() for item in package.relevant_test_results]
    return REPLAN_USER_PROMPT.format(
        project_name=project_name,
        objective=_escape_braces(objective.strip()),
        failed_task_id=package.failed_task.task_id,
        affected_branch=", ".join(branch) or package.failed_task.task_id,
        decision_summary=_escape_braces(package.decision_summary or "(none)"),
        last_error=_escape_braces(package.last_error or "(none)"),
        reduced_errors=_escape_braces("\n\n".join(errors) if errors else "(none)"),
        attempts=_escape_braces(str(attempts)),
        dag=_escape_braces("\n".join(dag_lines) if dag_lines else "(empty)"),
        existing_paths=_escape_braces(listing),
        module_contracts=_escape_braces(contracts),
    )


def _escape_braces(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def _inspect_to_worker(calls: Sequence[PlannerInspectCall]) -> list[WorkerToolCall]:
    return [
        WorkerToolCall(name=WorkerToolName(call.name.value), arguments=call.arguments)
        for call in calls
    ]


def _inspect_blocked_message(
    calls: Sequence[PlannerInspectCall],
    inspector: ToolDispatcher | None,
    inspect_turns: int,
) -> str:
    if not calls:
        return "Planner returned neither inspect_calls nor a plan"
    if inspector is None:
        return "Planner inspect tools are unavailable; return a plan"
    return f"Planner inspect budget exhausted after {inspect_turns} turn(s); return a plan"

"""Orchestrator agent: interpret failures. It does not own counters or DAG eligibility."""

from __future__ import annotations

from mobyai.agents.prompts.orchestrator import (
    DECISION_REPAIR_PROMPT,
    SYSTEM_PROMPT,
    USER_PROMPT,
)
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.inference import ChatMessage, ModelRequest
from mobyai.models.orchestrator import OrchestratorDecision, OrchestratorResult, TargetAgent
from mobyai.models.task import Task


class OrchestratorValidationError(ValueError):
    """Pydantic succeeded but deterministic orchestrator rules failed."""


class OrchestratorAgent:
    def __init__(
        self,
        generator: StructuredGenerator,
        *,
        max_decision_repairs: int | None = None,
    ) -> None:
        self._generator = generator
        self._max_decision_repairs = (
            generator.max_repair_attempts
            if max_decision_repairs is None
            else max(max_decision_repairs, 0)
        )

    async def decide(
        self,
        *,
        project_name: str,
        objective: str,
        task: Task,
        required_decision: OrchestratorDecision,
        attempt_count: int,
        max_task_attempts: int,
        reduced_error: str,
    ) -> OrchestratorResult:
        request = ModelRequest(
            messages=[
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=_render_user(
                        project_name=project_name,
                        objective=objective,
                        task=task,
                        required_decision=required_decision,
                        attempt_count=attempt_count,
                        max_task_attempts=max_task_attempts,
                        reduced_error=reduced_error,
                    ),
                ),
            ]
        )
        last_error: Exception | None = None
        for _attempt in range(self._max_decision_repairs + 1):
            result = await self._generator.generate(request, OrchestratorResult)
            try:
                return validate_orchestrator_output(
                    result,
                    expected=required_decision,
                    task_id=task.task_id,
                )
            except OrchestratorValidationError as exc:
                last_error = exc
                request = ModelRequest(
                    messages=[
                        *request.messages,
                        ChatMessage(role="assistant", content=result.model_dump_json()),
                        ChatMessage(
                            role="user",
                            content=DECISION_REPAIR_PROMPT.format(
                                errors=str(exc),
                                required_decision=required_decision.value,
                                task_id=task.task_id,
                            ),
                        ),
                    ]
                )
        raise OrchestratorValidationError(
            f"Orchestrator validation failed after {self._max_decision_repairs} "
            f"repair(s): {last_error}"
        ) from last_error


def validate_orchestrator_output(
    result: OrchestratorResult,
    *,
    expected: OrchestratorDecision,
    task_id: str,
) -> OrchestratorResult:
    if result.decision != expected:
        raise OrchestratorValidationError(
            f"Harness requires {expected.value}, orchestrator returned {result.decision.value}"
        )
    if result.task_id not in {None, task_id}:
        raise OrchestratorValidationError(
            f"Orchestrator task_id {result.task_id} does not match {task_id}"
        )
    updated = result.model_copy(update={"task_id": task_id})
    payload = updated.next_payload
    if expected is OrchestratorDecision.RETRY_WITH_FIX:
        if payload is None or not payload.instruction.strip():
            raise OrchestratorValidationError(
                "RETRY_WITH_FIX requires next_payload.instruction for the Worker"
            )
        if payload.target_agent is not TargetAgent.WORKER:
            raise OrchestratorValidationError(
                "RETRY_WITH_FIX next_payload.target_agent must be WORKER"
            )
    if (
        expected is OrchestratorDecision.ESCALATE_TO_PLANNER
        and payload is not None
        and payload.target_agent is not TargetAgent.PLANNER
    ):
        raise OrchestratorValidationError(
            "ESCALATE_TO_PLANNER next_payload.target_agent must be PLANNER"
        )
    return updated


def _render_user(
    *,
    project_name: str,
    objective: str,
    task: Task,
    required_decision: OrchestratorDecision,
    attempt_count: int,
    max_task_attempts: int,
    reduced_error: str,
) -> str:
    target = (
        TargetAgent.WORKER
        if required_decision is OrchestratorDecision.RETRY_WITH_FIX
        else TargetAgent.PLANNER
    )
    return USER_PROMPT.format(
        project_name=project_name,
        objective=objective.strip(),
        required_decision=required_decision.value,
        task_id=task.task_id,
        attempt_count=attempt_count,
        max_task_attempts=max_task_attempts,
        task_json=task.model_dump_json(),
        reduced_error=reduced_error.strip() or "(none)",
        target_agent=target.value,
    )

"""Orchestrator structured output contract."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OrchestratorDecision(StrEnum):
    CONTINUE = "CONTINUE"
    RETRY_WITH_FIX = "RETRY_WITH_FIX"
    ESCALATE_TO_PLANNER = "ESCALATE_TO_PLANNER"
    MISSION_COMPLETE = "MISSION_COMPLETE"
    BLOCKED = "BLOCKED"


class TargetAgent(StrEnum):
    WORKER = "WORKER"
    PLANNER = "PLANNER"


class NextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent: TargetAgent
    instruction: str = Field(min_length=1)
    required_files: list[str] = Field(default_factory=list)


class OrchestratorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_summary: str = Field(min_length=1)
    decision: OrchestratorDecision
    task_id: str | None = None
    next_payload: NextPayload | None = None

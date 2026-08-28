"""Checkpoint records persisted after meaningful harness transitions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CheckpointKind(StrEnum):
    PLAN_CREATED = "plan_created"
    TASK_DISPATCH = "task_dispatch"
    WORKER_EXECUTION = "worker_execution"
    VALIDATION = "validation"
    FAILURE = "failure"
    RETRY = "retry"
    ESCALATION = "escalation"
    REPLAN = "replan"
    TASK_COMPLETION = "task_completion"


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    kind: CheckpointKind
    created_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

"""Escalation package handed to the Planner. Built by the harness, not the model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mobyai.context.log_reducer import ReducedLog
from mobyai.models.attempt import TaskAttempt
from mobyai.models.task import Task


class EscalationPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failed_task: Task
    attempts: list[TaskAttempt] = Field(default_factory=list)
    relevant_test_results: list[ReducedLog] = Field(default_factory=list)
    affected_dependencies: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    last_error: str | None = None
    decision_summary: str | None = None

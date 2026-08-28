"""Task attempt history. The harness, not the model, owns retry accounting."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AttemptResult(StrEnum):
    IN_PROGRESS = "in_progress"
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    TIMEOUT = "timeout"


class TaskAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    project_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime | None = None
    worker_response: dict[str, Any] | None = None
    test_exit_code: int | None = None
    test_log_path: str | None = None
    result: AttemptResult | None = AttemptResult.IN_PROGRESS

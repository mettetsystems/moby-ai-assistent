"""Append-only structured events for observability."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    timestamp: datetime
    project_id: str | None = None
    task_id: str | None = None
    component: str = Field(min_length=1)
    event: str = Field(min_length=1)
    attempt: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

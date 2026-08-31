"""Harness-owned module surface. Extracted after tests pass, not by the Worker."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModuleContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    rendered: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime

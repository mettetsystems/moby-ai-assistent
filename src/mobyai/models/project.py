"""Project aggregate and project-level state."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectState(StrEnum):
    NEW = "NEW"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    READY = "READY"
    DISPATCHING = "DISPATCHING"
    WORKING = "WORKING"
    VALIDATING = "VALIDATING"
    RETRYING = "RETRYING"
    ESCALATING = "ESCALATING"
    REPLANNING = "REPLANNING"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    workspace_root: Path
    state: ProjectState = ProjectState.NEW
    paused_from: ProjectState | None = None
    architecture_summary: str | None = None
    directory_structure: list[str] = Field(default_factory=list)
    documentation_requirements: list[str] = Field(default_factory=list)
    current_task_id: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("workspace_root", mode="before")
    @classmethod
    def expand_workspace_root(cls, value: Path | str) -> Path:
        return Path(str(value)).expanduser()

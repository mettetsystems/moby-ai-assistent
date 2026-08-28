"""Reconstructed project execution state loaded from persistence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mobyai.models.attempt import TaskAttempt
from mobyai.models.checkpoint import Checkpoint
from mobyai.models.project import Project
from mobyai.models.task import Task


class ProjectSnapshot(BaseModel):
    """Everything needed to continue a project after restart. Not LLM memory."""

    model_config = ConfigDict(extra="forbid")

    project: Project
    tasks: list[Task] = Field(default_factory=list)
    attempts: list[TaskAttempt] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    latest_checkpoint: Checkpoint | None = None

"""Atomic DAG task."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    TESTING = "testing"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    target_files: list[str] = Field(default_factory=list)
    required_context: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    superseded_by: str | None = None

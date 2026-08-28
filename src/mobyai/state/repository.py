"""Persistence interfaces. SQLite and in-memory stores implement these."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from mobyai.models.attempt import TaskAttempt
from mobyai.models.checkpoint import Checkpoint
from mobyai.models.event import EventRecord
from mobyai.models.project import Project
from mobyai.models.snapshot import ProjectSnapshot
from mobyai.models.task import Task


class ProjectRepository(Protocol):
    async def get(self, project_id: str) -> Project | None: ...

    async def save(self, project: Project) -> None: ...

    async def list_ids(self) -> list[str]: ...


class TaskRepository(Protocol):
    async def get(self, project_id: str, task_id: str) -> Task | None: ...

    async def list_for_project(self, project_id: str) -> list[Task]: ...

    async def save(self, project_id: str, task: Task) -> None: ...

    async def save_all(self, project_id: str, tasks: Sequence[Task]) -> None: ...


class CheckpointRepository(Protocol):
    async def save(self, checkpoint: Checkpoint) -> None: ...

    async def latest(self, project_id: str) -> Checkpoint | None: ...

    async def list(self, project_id: str) -> list[Checkpoint]: ...


class AttemptRepository(Protocol):
    async def save(self, attempt: TaskAttempt) -> TaskAttempt: ...

    async def get(
        self, project_id: str, task_id: str, attempt_number: int
    ) -> TaskAttempt | None: ...

    async def list_for_task(self, project_id: str, task_id: str) -> list[TaskAttempt]: ...

    async def list_for_project(self, project_id: str) -> list[TaskAttempt]: ...


class EventRepository(Protocol):
    async def append(self, event: EventRecord) -> EventRecord: ...

    async def list_for_project(
        self, project_id: str, *, limit: int | None = None
    ) -> list[EventRecord]: ...


class StateStore(Protocol):
    projects: ProjectRepository
    tasks: TaskRepository
    checkpoints: CheckpointRepository
    attempts: AttemptRepository
    events: EventRepository

    async def load_snapshot(self, project_id: str) -> ProjectSnapshot | None: ...

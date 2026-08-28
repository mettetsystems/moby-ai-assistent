from datetime import UTC, datetime
from pathlib import Path

from mobyai.models.project import Project
from mobyai.models.task import Task, TaskStatus

FROZEN_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_project(**overrides: object) -> Project:
    payload: dict[str, object] = {
        "project_id": "proj-1",
        "name": "example",
        "objective": "Build an example application",
        "workspace_root": Path("/tmp/mobyai/example"),
        "created_at": FROZEN_NOW,
        "updated_at": FROZEN_NOW,
    }
    payload.update(overrides)
    return Project.model_validate(payload)


def make_task(task_id: str, **overrides: object) -> Task:
    payload: dict[str, object] = {
        "task_id": task_id,
        "name": f"Task {task_id}",
        "description": f"Do atomic work for {task_id}",
        "status": TaskStatus.PENDING,
    }
    payload.update(overrides)
    return Task.model_validate(payload)

"""Task graph: eligibility, blocking, and stable ready-task selection."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from mobyai.dag.validator import validate_dag
from mobyai.models.task import Task, TaskStatus

_BLOCKING_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.SUPERSEDED}
)
_ELIGIBLE_FOR_REFRESH = frozenset(
    {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED}
)


class TaskGraph:
    """In-memory DAG. Status refresh is deterministic and model-free."""

    def __init__(self, tasks: Sequence[Task], *, validate: bool = True) -> None:
        if validate:
            validate_dag(tasks)
        self._tasks: dict[str, Task] = {
            task.task_id: task.model_copy(deep=True) for task in tasks
        }
        self.refresh_statuses()

    def tasks(self) -> list[Task]:
        return [self._tasks[task_id].model_copy(deep=True) for task_id in self._ordered_ids()]

    def get(self, task_id: str) -> Task:
        return self._tasks[task_id].model_copy(deep=True)

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        current = self._tasks[task_id]
        self._tasks[task_id] = current.model_copy(update={"status": status})
        self.refresh_statuses()

    def increment_attempts(self, task_id: str) -> Task:
        current = self._tasks[task_id]
        updated = current.model_copy(update={"attempt_count": current.attempt_count + 1})
        self._tasks[task_id] = updated
        return updated.model_copy(deep=True)

    def refresh_statuses(self) -> None:
        for task_id in self._ordered_ids():
            task = self._tasks[task_id]
            if task.status not in _ELIGIBLE_FOR_REFRESH:
                continue
            self._tasks[task_id] = task.model_copy(
                update={"status": self._computed_status(task)}
            )

    def select_ready_tasks(self) -> list[Task]:
        ready = [
            self._tasks[task_id]
            for task_id in self._ordered_ids()
            if self._tasks[task_id].status == TaskStatus.READY
        ]
        return [task.model_copy(deep=True) for task in ready]

    def select_next_task(self) -> Task | None:
        ready = self.select_ready_tasks()
        return ready[0] if ready else None

    def blocked_tasks(self) -> list[Task]:
        return [
            self._tasks[task_id].model_copy(deep=True)
            for task_id in self._ordered_ids()
            if self._tasks[task_id].status == TaskStatus.BLOCKED
        ]

    def is_complete(self) -> bool:
        required = [
            task
            for task in self._tasks.values()
            if task.status != TaskStatus.SUPERSEDED
        ]
        return bool(required) and all(task.status == TaskStatus.PASSED for task in required)

    def _computed_status(self, task: Task) -> TaskStatus:
        dependencies = [self._tasks[dep_id] for dep_id in task.dependencies]
        if any(dep.status in _BLOCKING_STATUSES for dep in dependencies):
            return TaskStatus.BLOCKED
        if all(dep.status == TaskStatus.PASSED for dep in dependencies):
            return TaskStatus.READY
        return TaskStatus.PENDING

    def _ordered_ids(self) -> list[str]:
        return sorted(self._tasks)

    def __iter__(self) -> Iterable[Task]:
        return iter(self.tasks())

    def __len__(self) -> int:
        return len(self._tasks)

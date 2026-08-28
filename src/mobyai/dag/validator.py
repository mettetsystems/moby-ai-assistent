"""Structural DAG validation: uniqueness, dependency existence, acyclicity."""

from __future__ import annotations

from collections.abc import Sequence

from mobyai.models.task import Task


class DAGValidationError(ValueError):
    """Planner output or stored DAG is structurally invalid."""


def validate_dag(tasks: Sequence[Task]) -> None:
    seen: set[str] = set()
    by_id: dict[str, Task] = {}
    for task in tasks:
        if task.task_id in seen:
            raise DAGValidationError(f"duplicate task_id: {task.task_id}")
        seen.add(task.task_id)
        by_id[task.task_id] = task

    for task in tasks:
        for dependency in task.dependencies:
            if dependency not in by_id:
                raise DAGValidationError(
                    f"task {task.task_id} depends on unknown task {dependency}"
                )

    if _has_cycle(by_id):
        raise DAGValidationError("cycle detected in task dependencies")


def _has_cycle(by_id: dict[str, Task]) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task_id: WHITE for task_id in by_id}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for neighbor in by_id[node].dependencies:
            neighbor_color = color[neighbor]
            if neighbor_color == GRAY:
                return True
            if neighbor_color == WHITE and visit(neighbor):
                return True
        color[node] = BLACK
        return False

    return any(visit(task_id) for task_id, marker in color.items() if marker == WHITE)

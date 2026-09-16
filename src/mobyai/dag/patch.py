"""Apply a Planner DAG patch. PASSED tasks are never rewritten by the model."""

from __future__ import annotations

from collections.abc import Sequence

from mobyai.dag.validator import DAGValidationError, validate_dag
from mobyai.models.planner import DAGPatch, PlannerTaskSpec
from mobyai.models.task import Task, TaskStatus

_PROTECTED = frozenset({TaskStatus.PASSED})


class DAGPatchError(ValueError):
    """The patch is structurally invalid or touches a frozen branch."""


def affected_branch(tasks: Sequence[Task], failed_task_id: str) -> set[str]:
    """Failed node plus every transitive successor."""
    ids = {task.task_id for task in tasks}
    if failed_task_id not in ids:
        raise DAGPatchError(f"failed task is not in the DAG: {failed_task_id}")
    affected = {failed_task_id}
    changed = True
    while changed:
        changed = False
        for task in tasks:
            if task.task_id in affected:
                continue
            if any(dep in affected for dep in task.dependencies):
                affected.add(task.task_id)
                changed = True
    return affected


def replacement_sinks(replacements: Sequence[PlannerTaskSpec]) -> list[str]:
    ids = {spec.task_id for spec in replacements}
    depended_on = {
        dep
        for spec in replacements
        for dep in spec.dependencies
        if dep in ids
    }
    sinks = [spec.task_id for spec in replacements if spec.task_id not in depended_on]
    return sinks or [replacements[0].task_id]


def apply_dag_patch(
    tasks: Sequence[Task],
    patch: DAGPatch,
    *,
    failed_task_id: str,
) -> list[Task]:
    """Return a new task list. Does not mutate the inputs."""
    by_id = {task.task_id: task.model_copy(deep=True) for task in tasks}
    branch = affected_branch(tasks, failed_task_id)
    superseded_ids = _validate_patch(patch, by_id, failed_task_id, branch)

    sinks_by_old = {
        item.task_id: replacement_sinks(item.replacements) for item in patch.supersede
    }
    new_specs = [
        spec for item in patch.supersede for spec in item.replacements
    ]
    new_specs.extend(patch.add_tasks)
    new_ids = {spec.task_id for spec in new_specs}
    live_ids = (set(by_id) - superseded_ids) | new_ids

    for spec in new_specs:
        for dep in spec.dependencies:
            if dep in superseded_ids:
                raise DAGPatchError(
                    f"{spec.task_id} depends on superseded task {dep}; "
                    "depend on surviving or replacement tasks instead"
                )
            if dep not in live_ids:
                raise DAGPatchError(
                    f"{spec.task_id} depends on unknown task {dep}"
                )

    for item in patch.supersede:
        current = by_id[item.task_id]
        by_id[item.task_id] = current.model_copy(
            update={
                "status": TaskStatus.SUPERSEDED,
                "superseded_by": item.replacements[0].task_id,
            }
        )
        for spec in item.replacements:
            by_id[spec.task_id] = spec.to_task()
    for spec in patch.add_tasks:
        by_id[spec.task_id] = spec.to_task()

    updated: list[Task] = []
    for task in by_id.values():
        if task.task_id in new_ids or task.status is TaskStatus.SUPERSEDED:
            updated.append(task)
            continue
        rewritten = [
            sink
            for dep in task.dependencies
            for sink in (sinks_by_old.get(dep, [dep]))
        ]
        # drop dups, preserve order
        seen: set[str] = set()
        deps: list[str] = []
        for dep in rewritten:
            if dep not in seen:
                seen.add(dep)
                deps.append(dep)
        if deps != task.dependencies:
            updated.append(task.model_copy(update={"dependencies": deps}))
        else:
            updated.append(task)

    try:
        validate_dag(updated)
    except DAGValidationError as exc:
        raise DAGPatchError(str(exc)) from exc
    return updated


def _validate_patch(
    patch: DAGPatch,
    by_id: dict[str, Task],
    failed_task_id: str,
    branch: set[str],
) -> set[str]:
    if not any(item.task_id == failed_task_id for item in patch.supersede):
        raise DAGPatchError(f"patch must supersede failed task {failed_task_id}")

    superseded_ids: set[str] = set()
    new_ids: set[str] = set()
    for item in patch.supersede:
        if item.task_id in superseded_ids:
            raise DAGPatchError(f"duplicate supersede of {item.task_id}")
        existing = by_id.get(item.task_id)
        if existing is None:
            raise DAGPatchError(f"cannot supersede unknown task {item.task_id}")
        if existing.status is TaskStatus.SUPERSEDED:
            raise DAGPatchError(f"cannot supersede already superseded task {item.task_id}")
        if existing.status in _PROTECTED:
            raise DAGPatchError(f"cannot supersede PASSED task {item.task_id}")
        if item.task_id not in branch:
            raise DAGPatchError(
                f"cannot supersede {item.task_id}; it is outside the failed branch"
            )
        superseded_ids.add(item.task_id)
        for spec in item.replacements:
            _register_new_id(spec.task_id, by_id, new_ids, superseded_ids)
    for spec in patch.add_tasks:
        _register_new_id(spec.task_id, by_id, new_ids, superseded_ids)
    return superseded_ids


def _register_new_id(
    task_id: str,
    by_id: dict[str, Task],
    new_ids: set[str],
    superseded_ids: set[str],
) -> None:
    if task_id in new_ids:
        raise DAGPatchError(f"duplicate new task_id: {task_id}")
    if task_id in by_id and task_id not in superseded_ids:
        raise DAGPatchError(f"new task_id already exists: {task_id}")
    if task_id in superseded_ids:
        raise DAGPatchError(
            f"replacement must use a new task_id, not the superseded id {task_id}"
        )
    new_ids.add(task_id)

import pytest

from mobyai.dag.patch import DAGPatchError, affected_branch, apply_dag_patch
from mobyai.models.planner import DAGPatch, PlannerTaskSpec, TaskSupersede
from mobyai.models.task import TaskStatus
from tests.helpers import make_task


def _spec(task_id: str, **overrides: object) -> PlannerTaskSpec:
    payload: dict[str, object] = {
        "task_id": task_id,
        "name": task_id,
        "description": f"Replace {task_id}",
        "acceptance_criteria": ["observable"],
        "test_commands": ["pytest"],
    }
    payload.update(overrides)
    return PlannerTaskSpec.model_validate(payload)


def _patch(*supersede: TaskSupersede, add: list[PlannerTaskSpec] | None = None) -> DAGPatch:
    return DAGPatch(
        summary="Replace the failed branch",
        supersede=list(supersede),
        add_tasks=add or [],
    )


def test_affected_branch_includes_transitive_successors() -> None:
    tasks = [
        make_task("STEP_000", status=TaskStatus.PASSED),
        make_task("STEP_001", status=TaskStatus.FAILED),
        make_task("STEP_002", dependencies=["STEP_001"], status=TaskStatus.BLOCKED),
        make_task("STEP_003", dependencies=["STEP_002"], status=TaskStatus.BLOCKED),
        make_task("STEP_009", status=TaskStatus.READY),
    ]
    assert affected_branch(tasks, "STEP_001") == {"STEP_001", "STEP_002", "STEP_003"}


def test_apply_supersedes_failed_node_and_retargets_dependents() -> None:
    tasks = [
        make_task("STEP_000", status=TaskStatus.PASSED),
        make_task("STEP_001", status=TaskStatus.FAILED, attempt_count=2),
        make_task("STEP_002", dependencies=["STEP_001"], status=TaskStatus.BLOCKED),
    ]
    patch = _patch(
        TaskSupersede(
            task_id="STEP_001",
            replacements=[_spec("STEP_001B", dependencies=["STEP_000"])],
        )
    )
    result = apply_dag_patch(tasks, patch, failed_task_id="STEP_001")
    by_id = {task.task_id: task for task in result}
    assert by_id["STEP_000"].status == TaskStatus.PASSED
    assert by_id["STEP_001"].status == TaskStatus.SUPERSEDED
    assert by_id["STEP_001"].superseded_by == "STEP_001B"
    assert by_id["STEP_001B"].dependencies == ["STEP_000"]
    assert by_id["STEP_002"].dependencies == ["STEP_001B"]
    assert tasks[1].status == TaskStatus.FAILED


def test_cannot_supersede_passed_task() -> None:
    tasks = [
        make_task("STEP_000", status=TaskStatus.PASSED),
        make_task("STEP_001", status=TaskStatus.FAILED),
    ]
    patch = _patch(
        TaskSupersede(task_id="STEP_001", replacements=[_spec("STEP_001B")]),
        TaskSupersede(task_id="STEP_000", replacements=[_spec("STEP_000B")]),
    )
    with pytest.raises(DAGPatchError, match="PASSED"):
        apply_dag_patch(tasks, patch, failed_task_id="STEP_001")


def test_must_supersede_failed_task() -> None:
    tasks = [
        make_task("STEP_001", status=TaskStatus.FAILED),
        make_task("STEP_002", dependencies=["STEP_001"], status=TaskStatus.BLOCKED),
    ]
    patch = _patch(TaskSupersede(task_id="STEP_002", replacements=[_spec("STEP_002B")]))
    with pytest.raises(DAGPatchError, match="must supersede failed task"):
        apply_dag_patch(tasks, patch, failed_task_id="STEP_001")


def test_cannot_reuse_existing_or_superseded_ids() -> None:
    tasks = [
        make_task("STEP_001", status=TaskStatus.FAILED),
        make_task("STEP_002", status=TaskStatus.READY),
    ]
    patch = _patch(TaskSupersede(task_id="STEP_001", replacements=[_spec("STEP_002")]))
    with pytest.raises(DAGPatchError, match="already exists"):
        apply_dag_patch(tasks, patch, failed_task_id="STEP_001")
    patch = _patch(TaskSupersede(task_id="STEP_001", replacements=[_spec("STEP_001")]))
    with pytest.raises(DAGPatchError, match="new task_id"):
        apply_dag_patch(tasks, patch, failed_task_id="STEP_001")


def test_cannot_supersede_outside_failed_branch() -> None:
    tasks = [
        make_task("STEP_001", status=TaskStatus.FAILED),
        make_task("STEP_009", status=TaskStatus.READY),
    ]
    patch = _patch(
        TaskSupersede(task_id="STEP_001", replacements=[_spec("STEP_001B")]),
        TaskSupersede(task_id="STEP_009", replacements=[_spec("STEP_009B")]),
    )
    with pytest.raises(DAGPatchError, match="outside the failed branch"):
        apply_dag_patch(tasks, patch, failed_task_id="STEP_001")


def test_is_complete_ignores_superseded() -> None:
    from mobyai.dag.graph import TaskGraph

    tasks = [
        make_task("STEP_001", status=TaskStatus.SUPERSEDED, superseded_by="STEP_001B"),
        make_task("STEP_001B", status=TaskStatus.PASSED),
    ]
    graph = TaskGraph(tasks, validate=True)
    assert graph.is_complete() is True


def test_cannot_replace_a_superseded_task_again():
    tasks = [
        make_task("OLD", status=TaskStatus.SUPERSEDED, superseded_by="NEW"),
        make_task("NEW", status=TaskStatus.FAILED),
        make_task("DOWNSTREAM", dependencies=["NEW"], status=TaskStatus.BLOCKED),
    ]
    patch = _patch(TaskSupersede(task_id="OLD", replacements=[_spec("OTHER")]))
    with pytest.raises(DAGPatchError, match="already superseded"):
        apply_dag_patch(tasks, patch, failed_task_id="OLD")

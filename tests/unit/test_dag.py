import pytest

from mobyai.dag import DAGValidationError, TaskGraph, validate_dag
from mobyai.models.task import TaskStatus
from tests.helpers import make_task


def test_independent_tasks_become_ready() -> None:
    graph = TaskGraph([make_task("STEP_002"), make_task("STEP_001")])
    ready = graph.select_ready_tasks()
    assert [task.task_id for task in ready] == ["STEP_001", "STEP_002"]
    assert graph.select_next_task().task_id == "STEP_001"


def test_dependent_task_stays_pending_until_dependency_passes() -> None:
    graph = TaskGraph(
        [
            make_task("STEP_001"),
            make_task("STEP_002", dependencies=["STEP_001"]),
        ]
    )
    assert graph.get("STEP_001").status == TaskStatus.READY
    assert graph.get("STEP_002").status == TaskStatus.PENDING
    graph.set_status("STEP_001", TaskStatus.PASSED)
    assert graph.get("STEP_002").status == TaskStatus.READY
    assert graph.select_next_task().task_id == "STEP_002"


def test_failed_dependency_blocks_successors() -> None:
    graph = TaskGraph(
        [
            make_task("STEP_001"),
            make_task("STEP_002", dependencies=["STEP_001"]),
        ]
    )
    graph.set_status("STEP_001", TaskStatus.FAILED)
    assert graph.get("STEP_002").status == TaskStatus.BLOCKED
    assert graph.blocked_tasks()[0].task_id == "STEP_002"
    assert graph.select_next_task() is None


def test_blocked_and_superseded_dependencies_block_successors() -> None:
    graph = TaskGraph(
        [
            make_task("A"),
            make_task("B", dependencies=["A"]),
            make_task("C", dependencies=["A"]),
        ]
    )
    graph.set_status("A", TaskStatus.SUPERSEDED)
    assert graph.get("B").status == TaskStatus.BLOCKED
    assert graph.get("C").status == TaskStatus.BLOCKED


def test_cycle_is_rejected() -> None:
    tasks = [
        make_task("A", dependencies=["B"]),
        make_task("B", dependencies=["A"]),
    ]
    with pytest.raises(DAGValidationError, match="cycle"):
        validate_dag(tasks)
    with pytest.raises(DAGValidationError, match="cycle"):
        TaskGraph(tasks)


def test_self_dependency_is_a_cycle() -> None:
    with pytest.raises(DAGValidationError, match="cycle"):
        TaskGraph([make_task("A", dependencies=["A"])])


def test_missing_dependency_is_rejected() -> None:
    with pytest.raises(DAGValidationError, match="unknown task"):
        TaskGraph([make_task("A", dependencies=["missing"])])


def test_duplicate_task_ids_are_rejected() -> None:
    with pytest.raises(DAGValidationError, match="duplicate"):
        TaskGraph([make_task("A"), make_task("A")])


def test_running_status_is_not_overwritten_by_refresh() -> None:
    graph = TaskGraph([make_task("STEP_001")])
    graph.set_status("STEP_001", TaskStatus.RUNNING)
    graph.refresh_statuses()
    assert graph.get("STEP_001").status == TaskStatus.RUNNING


def test_is_complete_requires_all_non_superseded_passed() -> None:
    graph = TaskGraph(
        [
            make_task("STEP_001"),
            make_task("STEP_002", dependencies=["STEP_001"]),
        ]
    )
    assert graph.is_complete() is False
    graph.set_status("STEP_001", TaskStatus.PASSED)
    graph.set_status("STEP_002", TaskStatus.PASSED)
    assert graph.is_complete() is True

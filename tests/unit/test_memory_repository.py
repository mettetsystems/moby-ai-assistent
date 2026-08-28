from datetime import timedelta

from mobyai.engine.recovery import resume_project
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.project import ProjectState
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import FROZEN_NOW, make_project, make_task


async def test_project_save_and_reload() -> None:
    store = InMemoryStateStore()
    original = make_project(state=ProjectState.WORKING, current_task_id="STEP_001")
    await store.projects.save(original)
    original.state = ProjectState.FAILED
    loaded = await store.projects.get("proj-1")
    assert loaded is not None
    assert loaded.state == ProjectState.WORKING
    assert loaded.current_task_id == "STEP_001"
    assert await store.projects.list_ids() == ["proj-1"]


async def test_task_save_all_and_list() -> None:
    store = InMemoryStateStore()
    tasks = [make_task("STEP_002"), make_task("STEP_001")]
    await store.tasks.save_all("proj-1", tasks)
    listed = await store.tasks.list_for_project("proj-1")
    assert [task.task_id for task in listed] == ["STEP_001", "STEP_002"]
    fetched = await store.tasks.get("proj-1", "STEP_001")
    assert fetched is not None
    fetched.attempt_count = 9
    reread = await store.tasks.get("proj-1", "STEP_001")
    assert reread is not None
    assert reread.attempt_count == 0


async def test_checkpoint_latest_and_list() -> None:
    store = InMemoryStateStore()
    first = Checkpoint(
        checkpoint_id="cp-1",
        project_id="proj-1",
        kind=CheckpointKind.PLAN_CREATED,
        created_at=FROZEN_NOW,
        payload={"tasks": 2},
    )
    second = Checkpoint(
        checkpoint_id="cp-2",
        project_id="proj-1",
        kind=CheckpointKind.TASK_DISPATCH,
        created_at=FROZEN_NOW + timedelta(minutes=5),
        payload={"task_id": "STEP_001"},
    )
    await store.checkpoints.save(first)
    await store.checkpoints.save(second)
    latest = await store.checkpoints.latest("proj-1")
    assert latest is not None
    assert latest.checkpoint_id == "cp-2"
    assert [item.kind for item in await store.checkpoints.list("proj-1")] == [
        CheckpointKind.PLAN_CREATED,
        CheckpointKind.TASK_DISPATCH,
    ]


async def test_resume_interrupted_task_from_store() -> None:
    store = InMemoryStateStore()
    await store.projects.save(
        make_project(
            state=ProjectState.WORKING,
            current_task_id="STEP_004",
            last_error="process crashed",
            paused_from=None,
        )
    )
    await store.tasks.save(
        "proj-1",
        make_task("STEP_004", attempt_count=1),
    )
    reloaded = await store.projects.get("proj-1")
    task = await store.tasks.get("proj-1", "STEP_004")
    assert reloaded is not None
    assert reloaded.state == ProjectState.WORKING
    assert reloaded.current_task_id == "STEP_004"
    assert task is not None
    assert task.attempt_count == 1


async def test_missing_records_return_none() -> None:
    store = InMemoryStateStore()
    assert await store.projects.get("missing") is None
    assert await store.tasks.get("missing", "STEP_001") is None
    assert await store.checkpoints.latest("missing") is None
    assert await store.load_snapshot("missing") is None


async def test_load_snapshot_includes_attempts() -> None:
    store = InMemoryStateStore()
    await store.projects.save(
        make_project(state=ProjectState.WORKING, current_task_id="STEP_004")
    )
    await store.tasks.save("proj-1", make_task("STEP_004", attempt_count=1))
    await store.attempts.save(
        TaskAttempt(
            project_id="proj-1",
            task_id="STEP_004",
            attempt_number=1,
            started_at=FROZEN_NOW,
            result=AttemptResult.IN_PROGRESS,
        )
    )
    snapshot = await resume_project(store, "proj-1")
    assert snapshot.project.state == ProjectState.WORKING
    assert snapshot.attempts[0].attempt_number == 1

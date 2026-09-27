import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from mobyai.engine.recovery import ProjectNotFoundError, resume_project
from mobyai.engine.state_machine import ProjectStateMachine
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.event import EventRecord
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.state.sqlite import PersistenceError, SqliteStateStore
from tests.helpers import FROZEN_NOW, make_project, make_task


async def _seed_interrupted(store: SqliteStateStore) -> None:
    await store.projects.save(
        make_project(
            state=ProjectState.WORKING,
            current_task_id="STEP_004",
            last_error="process crashed",
            architecture_summary="Small service",
            directory_structure=["src/", "tests/"],
        )
    )
    await store.tasks.save(
        "proj-1",
        make_task(
            "STEP_004",
            status=TaskStatus.RUNNING,
            attempt_count=1,
            target_files=["src/db.py"],
            dependencies=["STEP_001"],
        ),
    )
    await store.attempts.save(
        TaskAttempt(
            project_id="proj-1",
            task_id="STEP_004",
            attempt_number=1,
            started_at=FROZEN_NOW,
            result=AttemptResult.IN_PROGRESS,
        )
    )
    await store.checkpoints.save(
        Checkpoint(
            checkpoint_id="cp-dispatch",
            project_id="proj-1",
            kind=CheckpointKind.TASK_DISPATCH,
            created_at=FROZEN_NOW,
            payload={"task_id": "STEP_004"},
        )
    )


async def test_initialize_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "mobyai.db"
    store = SqliteStateStore(db_path)
    await store.initialize()
    await store.close()

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "schema_migrations",
        "projects",
        "tasks",
        "task_attempts",
        "checkpoints",
        "events",
        "module_contracts",
        "memory_nodes",
        "memory_edges",
        "memory_search",
    }.issubset(tables)
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [1, 2, 3, 4]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


async def test_project_and_task_round_trip(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        original = make_project(
            state=ProjectState.PLAN_READY,
            paused_from=None,
            architecture_summary="Layered",
        )
        await store.projects.save(original)
        original.state = ProjectState.FAILED
        loaded = await store.projects.get("proj-1")
        assert loaded is not None
        assert loaded.state == ProjectState.PLAN_READY
        assert loaded.architecture_summary == "Layered"
        assert await store.projects.list_ids() == ["proj-1"]

        await store.tasks.save_all(
            "proj-1",
            [make_task("STEP_002"), make_task("STEP_001", target_files=["src/a.py"])],
        )
        listed = await store.tasks.list_for_project("proj-1")
        assert [task.task_id for task in listed] == ["STEP_001", "STEP_002"]
        fetched = await store.tasks.get("proj-1", "STEP_001")
        assert fetched is not None
        fetched.attempt_count = 9
        reread = await store.tasks.get("proj-1", "STEP_001")
        assert reread is not None
        assert reread.attempt_count == 0
        assert reread.target_files == ["src/a.py"]


async def test_checkpoint_latest(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        await store.projects.save(make_project())
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
        assert latest.payload["task_id"] == "STEP_001"


async def test_attempt_history_and_events(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        await store.projects.save(make_project())
        await store.tasks.save("proj-1", make_task("STEP_001", attempt_count=1))
        saved = await store.attempts.save(
            TaskAttempt(
                project_id="proj-1",
                task_id="STEP_001",
                attempt_number=1,
                started_at=FROZEN_NOW,
                finished_at=FROZEN_NOW + timedelta(seconds=12),
                worker_response={"summary": "wrote src/db.py", "done": True},
                test_exit_code=1,
                test_log_path="artifacts/logs/STEP_001/attempt_001.log",
                result=AttemptResult.FAIL,
            )
        )
        assert saved.id is not None
        updated = await store.attempts.save(
            saved.model_copy(update={"result": AttemptResult.PASS, "test_exit_code": 0})
        )
        assert updated.id == saved.id
        history = await store.attempts.list_for_task("proj-1", "STEP_001")
        assert len(history) == 1
        assert history[0].result == AttemptResult.PASS
        assert history[0].worker_response is not None
        assert history[0].worker_response["summary"] == "wrote src/db.py"

        event = await store.events.append(
            EventRecord(
                timestamp=FROZEN_NOW,
                project_id="proj-1",
                task_id="STEP_001",
                component="worker",
                event="task_completed",
                attempt=1,
            )
        )
        assert event.id is not None
        listed = await store.events.list_for_project("proj-1")
        assert listed[0].event == "task_completed"


async def test_resume_after_reconnect(tmp_path: Path) -> None:
    db_path = tmp_path / "mobyai.db"
    store = SqliteStateStore(db_path)
    await store.initialize()
    await _seed_interrupted(store)
    await store.close()

    restarted = SqliteStateStore(db_path)
    snapshot = await resume_project(restarted, "proj-1")
    await restarted.close()

    assert snapshot.project.state == ProjectState.WORKING
    assert snapshot.project.current_task_id == "STEP_004"
    assert snapshot.project.last_error == "process crashed"
    assert snapshot.tasks[0].status == TaskStatus.RUNNING
    assert snapshot.tasks[0].attempt_count == 1
    assert snapshot.attempts[0].result == AttemptResult.IN_PROGRESS
    assert snapshot.latest_checkpoint is not None
    assert snapshot.latest_checkpoint.kind == CheckpointKind.TASK_DISPATCH


async def test_resume_paused_project_keeps_paused_from(tmp_path: Path) -> None:
    db_path = tmp_path / "mobyai.db"
    async with SqliteStateStore(db_path) as store:
        paused = ProjectStateMachine.pause(
            make_project(state=ProjectState.VALIDATING), now=FROZEN_NOW
        )
        await store.projects.save(paused)

    async with SqliteStateStore(db_path) as store:
        snapshot = await resume_project(store, "proj-1")

    assert snapshot.project.state == ProjectState.PAUSED
    assert snapshot.project.paused_from == ProjectState.VALIDATING
    resumed = ProjectStateMachine.resume(snapshot.project, now=FROZEN_NOW)
    assert resumed.state == ProjectState.VALIDATING


async def test_missing_project_resume_raises(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        with pytest.raises(ProjectNotFoundError):
            await resume_project(store, "missing")


async def test_task_requires_existing_project(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        with pytest.raises(PersistenceError):
            await store.tasks.save("missing", make_task("STEP_001"))


async def test_missing_records_return_none(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        assert await store.projects.get("missing") is None
        assert await store.tasks.get("missing", "STEP_001") is None
        assert await store.checkpoints.latest("missing") is None
        assert await store.load_snapshot("missing") is None


async def test_module_contract_round_trip(tmp_path: Path) -> None:
    from mobyai.models.contract import ModuleContract

    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        await store.projects.save(make_project())
        contract = ModuleContract(
            project_id="proj-1",
            path="src/db.py",
            task_id="STEP_001",
            rendered="src/db.py\ndef connect() -> int",
            payload={"functions": [{"name": "connect", "args": [], "returns": "int"}]},
            updated_at=FROZEN_NOW,
        )
        await store.contracts.upsert(contract)
        loaded = await store.contracts.get("proj-1", "src/db.py")
        assert loaded is not None
        assert loaded.rendered == contract.rendered
        assert loaded.payload["functions"][0]["name"] == "connect"
        listed = await store.contracts.list_for_project("proj-1")
        assert [item.path for item in listed] == ["src/db.py"]
        updated = contract.model_copy(
            update={"rendered": "src/db.py\ndef connect(dsn: str) -> int", "task_id": "STEP_001B"}
        )
        await store.contracts.upsert(updated)
        again = await store.contracts.get("proj-1", "src/db.py")
        assert again is not None
        assert again.task_id == "STEP_001B"
        assert "dsn: str" in again.rendered


async def test_v2_migrates_existing_v1_database(tmp_path: Path) -> None:
    from mobyai.state.migrations import SCHEMA_V1, apply_migrations

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_V1)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
        (FROZEN_NOW.isoformat(),),
    )
    conn.commit()
    conn.close()

    async with SqliteStateStore(db_path) as store:
        await store.projects.save(make_project())
        assert await store.contracts.list_for_project("proj-1") == []

    conn = sqlite3.connect(db_path)
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert 2 in versions
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "module_contracts" in tables
    conn.close()

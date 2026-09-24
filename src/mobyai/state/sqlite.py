"""SQLite persistence. stdlib sqlite3 with an asyncio lock around thread hops."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.checkpoint import Checkpoint, CheckpointKind
from mobyai.models.contract import ModuleContract
from mobyai.models.event import EventRecord
from mobyai.models.memory import MemoryEdge, MemoryEdgeKind, MemoryKind, MemoryLayer, MemoryNode
from mobyai.models.project import Project, ProjectState
from mobyai.models.snapshot import ProjectSnapshot
from mobyai.models.task import Task, TaskStatus
from mobyai.state.migrations import apply_migrations

T = TypeVar("T")


class PersistenceError(Exception):
    """Constraint or I/O failure in the state backend."""


def _dump_json(value: Any) -> str:
    return json.dumps(value, default=str)


def _load_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _dt_to_str(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_dt_optional(value: str | None) -> datetime | None:
    return None if value is None else _parse_dt(value)


class SqliteDatabase:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self.run(lambda _conn: None)

    async def close(self) -> None:
        async with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                await asyncio.to_thread(conn.close)

    async def run(self, func: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(self._run_sync, func)

    def _run_sync(self, func: Callable[[sqlite3.Connection], T]) -> T:
        try:
            return func(self._ensure_connection())
        except sqlite3.IntegrityError as exc:
            raise PersistenceError(str(exc)) from exc

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            apply_migrations(conn)
            self._conn = conn
        return self._conn


def _project_from_row(row: sqlite3.Row) -> Project:
    paused = row["paused_from"]
    return Project(
        project_id=row["project_id"],
        name=row["name"],
        objective=row["objective"],
        workspace_root=Path(row["workspace_root"]),
        state=ProjectState(row["state"]),
        paused_from=ProjectState(paused) if paused else None,
        architecture_summary=row["architecture_summary"],
        directory_structure=_load_json(row["directory_structure"], []),
        documentation_requirements=_load_json(row["documentation_requirements"], []),
        current_task_id=row["current_task_id"],
        last_error=row["last_error"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        name=row["name"],
        description=row["description"],
        target_files=_load_json(row["target_files"], []),
        required_context=_load_json(row["required_context"], []),
        dependencies=_load_json(row["dependencies"], []),
        acceptance_criteria=_load_json(row["acceptance_criteria"], []),
        test_files=_load_json(row["test_files"], []),
        test_commands=_load_json(row["test_commands"], []),
        status=TaskStatus(row["status"]),
        attempt_count=row["attempt_count"],
        superseded_by=row["superseded_by"],
    )


def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=row["checkpoint_id"],
        project_id=row["project_id"],
        kind=CheckpointKind(row["kind"]),
        created_at=_parse_dt(row["created_at"]),
        payload=_load_json(row["payload"], {}),
    )


def _attempt_from_row(row: sqlite3.Row) -> TaskAttempt:
    result = row["result"]
    worker_response = _load_json(row["worker_response"], None)
    return TaskAttempt(
        id=row["id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        attempt_number=row["attempt_number"],
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt_optional(row["finished_at"]),
        worker_response=worker_response,
        test_exit_code=row["test_exit_code"],
        test_log_path=row["test_log_path"],
        result=AttemptResult(result) if result else None,
    )


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        timestamp=_parse_dt(row["timestamp"]),
        project_id=row["project_id"],
        task_id=row["task_id"],
        component=row["component"],
        event=row["event"],
        attempt=row["attempt"],
        payload=_load_json(row["payload"], {}),
    )


def _optional_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return _parse_dt(value)


def _memory_node_from_row(row: sqlite3.Row) -> MemoryNode:
    return MemoryNode(
        node_id=row["node_id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        kind=MemoryKind(row["kind"]),
        layer=MemoryLayer(row["layer"]),
        name=row["name"],
        body=row["body"],
        fingerprint=row["fingerprint"],
        asserted_at=_parse_dt(row["asserted_at"]),
        retracted_at=_optional_dt(row["retracted_at"]),
    )


def _memory_edge_from_row(row: sqlite3.Row) -> MemoryEdge:
    return MemoryEdge(
        edge_id=row["edge_id"],
        project_id=row["project_id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        kind=MemoryEdgeKind(row["kind"]),
        asserted_at=_parse_dt(row["asserted_at"]),
        retracted_at=_optional_dt(row["retracted_at"]),
    )


def _fts_query(text: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", text)
    return " OR ".join(f'"{term}"' for term in terms[:12])


def _contract_from_row(row: sqlite3.Row) -> ModuleContract:
    payload = _load_json(row["payload"], {})
    if not isinstance(payload, dict):
        payload = {}
    return ModuleContract(
        project_id=row["project_id"],
        path=row["path"],
        task_id=row["task_id"],
        rendered=row["rendered"],
        payload=payload,
        updated_at=_parse_dt(row["updated_at"]),
    )


def _upsert_project(conn: sqlite3.Connection, project: Project) -> None:
    conn.execute(
        """
        INSERT INTO projects (
            project_id, name, objective, workspace_root, state, paused_from,
            architecture_summary, directory_structure, documentation_requirements,
            current_task_id, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            name = excluded.name,
            objective = excluded.objective,
            workspace_root = excluded.workspace_root,
            state = excluded.state,
            paused_from = excluded.paused_from,
            architecture_summary = excluded.architecture_summary,
            directory_structure = excluded.directory_structure,
            documentation_requirements = excluded.documentation_requirements,
            current_task_id = excluded.current_task_id,
            last_error = excluded.last_error,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
        """,
        (
            project.project_id,
            project.name,
            project.objective,
            str(project.workspace_root),
            project.state.value,
            project.paused_from.value if project.paused_from else None,
            project.architecture_summary,
            _dump_json(project.directory_structure),
            _dump_json(project.documentation_requirements),
            project.current_task_id,
            project.last_error,
            _dt_to_str(project.created_at),
            _dt_to_str(project.updated_at),
        ),
    )


def _upsert_task(conn: sqlite3.Connection, project_id: str, task: Task) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            project_id, task_id, name, description, target_files, required_context,
            dependencies, acceptance_criteria, test_files, test_commands, status,
            attempt_count, superseded_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, task_id) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            target_files = excluded.target_files,
            required_context = excluded.required_context,
            dependencies = excluded.dependencies,
            acceptance_criteria = excluded.acceptance_criteria,
            test_files = excluded.test_files,
            test_commands = excluded.test_commands,
            status = excluded.status,
            attempt_count = excluded.attempt_count,
            superseded_by = excluded.superseded_by
        """,
        (
            project_id,
            task.task_id,
            task.name,
            task.description,
            _dump_json(task.target_files),
            _dump_json(task.required_context),
            _dump_json(task.dependencies),
            _dump_json(task.acceptance_criteria),
            _dump_json(task.test_files),
            _dump_json(task.test_commands),
            task.status.value,
            task.attempt_count,
            task.superseded_by,
        ),
    )


class SqliteProjectRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, project_id: str) -> Project | None:
        def query(conn: sqlite3.Connection) -> Project | None:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return _project_from_row(row) if row is not None else None

        return await self._db.run(query)

    async def save(self, project: Project) -> None:
        def write(conn: sqlite3.Connection) -> None:
            _upsert_project(conn, project)
            conn.commit()

        await self._db.run(write)

    async def list_ids(self) -> list[str]:
        def query(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT project_id FROM projects ORDER BY project_id"
            ).fetchall()
            return [row["project_id"] for row in rows]

        return await self._db.run(query)


class SqliteTaskRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, project_id: str, task_id: str) -> Task | None:
        def query(conn: sqlite3.Connection) -> Task | None:
            row = conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND task_id = ?",
                (project_id, task_id),
            ).fetchone()
            return _task_from_row(row) if row is not None else None

        return await self._db.run(query)

    async def list_for_project(self, project_id: str) -> list[Task]:
        def query(conn: sqlite3.Connection) -> list[Task]:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY task_id",
                (project_id,),
            ).fetchall()
            return [_task_from_row(row) for row in rows]

        return await self._db.run(query)

    async def save(self, project_id: str, task: Task) -> None:
        def write(conn: sqlite3.Connection) -> None:
            _upsert_task(conn, project_id, task)
            conn.commit()

        await self._db.run(write)

    async def save_all(self, project_id: str, tasks: Sequence[Task]) -> None:
        def write(conn: sqlite3.Connection) -> None:
            for task in tasks:
                _upsert_task(conn, project_id, task)
            conn.commit()

        await self._db.run(write)


class SqliteCheckpointRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def save(self, checkpoint: Checkpoint) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, project_id, kind, created_at, payload
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    kind = excluded.kind,
                    created_at = excluded.created_at,
                    payload = excluded.payload
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.project_id,
                    checkpoint.kind.value,
                    _dt_to_str(checkpoint.created_at),
                    _dump_json(checkpoint.payload),
                ),
            )
            conn.commit()

        await self._db.run(write)

    async def latest(self, project_id: str) -> Checkpoint | None:
        def query(conn: sqlite3.Connection) -> Checkpoint | None:
            row = conn.execute(
                """
                SELECT * FROM checkpoints
                WHERE project_id = ?
                ORDER BY created_at DESC, checkpoint_id DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            return _checkpoint_from_row(row) if row is not None else None

        return await self._db.run(query)

    async def list(self, project_id: str) -> list[Checkpoint]:
        def query(conn: sqlite3.Connection) -> list[Checkpoint]:
            rows = conn.execute(
                """
                SELECT * FROM checkpoints
                WHERE project_id = ?
                ORDER BY created_at ASC, checkpoint_id ASC
                """,
                (project_id,),
            ).fetchall()
            return [_checkpoint_from_row(row) for row in rows]

        return await self._db.run(query)


class SqliteAttemptRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def save(self, attempt: TaskAttempt) -> TaskAttempt:
        def write(conn: sqlite3.Connection) -> TaskAttempt:
            conn.execute(
                """
                INSERT INTO task_attempts (
                    project_id, task_id, attempt_number, started_at, finished_at,
                    worker_response, test_exit_code, test_log_path, result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, task_id, attempt_number) DO UPDATE SET
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    worker_response = excluded.worker_response,
                    test_exit_code = excluded.test_exit_code,
                    test_log_path = excluded.test_log_path,
                    result = excluded.result
                """,
                (
                    attempt.project_id,
                    attempt.task_id,
                    attempt.attempt_number,
                    _dt_to_str(attempt.started_at),
                    _dt_to_str(attempt.finished_at) if attempt.finished_at else None,
                    _dump_json(attempt.worker_response)
                    if attempt.worker_response is not None
                    else None,
                    attempt.test_exit_code,
                    attempt.test_log_path,
                    attempt.result.value if attempt.result else None,
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT * FROM task_attempts
                WHERE project_id = ? AND task_id = ? AND attempt_number = ?
                """,
                (attempt.project_id, attempt.task_id, attempt.attempt_number),
            ).fetchone()
            assert row is not None
            return _attempt_from_row(row)

        return await self._db.run(write)

    async def get(
        self, project_id: str, task_id: str, attempt_number: int
    ) -> TaskAttempt | None:
        def query(conn: sqlite3.Connection) -> TaskAttempt | None:
            row = conn.execute(
                """
                SELECT * FROM task_attempts
                WHERE project_id = ? AND task_id = ? AND attempt_number = ?
                """,
                (project_id, task_id, attempt_number),
            ).fetchone()
            return _attempt_from_row(row) if row is not None else None

        return await self._db.run(query)

    async def list_for_task(self, project_id: str, task_id: str) -> list[TaskAttempt]:
        def query(conn: sqlite3.Connection) -> list[TaskAttempt]:
            rows = conn.execute(
                """
                SELECT * FROM task_attempts
                WHERE project_id = ? AND task_id = ?
                ORDER BY attempt_number
                """,
                (project_id, task_id),
            ).fetchall()
            return [_attempt_from_row(row) for row in rows]

        return await self._db.run(query)

    async def list_for_project(self, project_id: str) -> list[TaskAttempt]:
        def query(conn: sqlite3.Connection) -> list[TaskAttempt]:
            rows = conn.execute(
                """
                SELECT * FROM task_attempts
                WHERE project_id = ?
                ORDER BY task_id, attempt_number
                """,
                (project_id,),
            ).fetchall()
            return [_attempt_from_row(row) for row in rows]

        return await self._db.run(query)


class SqliteEventRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def append(self, event: EventRecord) -> EventRecord:
        def write(conn: sqlite3.Connection) -> EventRecord:
            cursor = conn.execute(
                """
                INSERT INTO events (
                    timestamp, project_id, task_id, component, event, attempt, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _dt_to_str(event.timestamp),
                    event.project_id,
                    event.task_id,
                    event.component,
                    event.event,
                    event.attempt,
                    _dump_json(event.payload),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            assert row is not None
            return _event_from_row(row)

        return await self._db.run(write)

    async def list_for_project(
        self, project_id: str, *, limit: int | None = None
    ) -> list[EventRecord]:
        def query(conn: sqlite3.Connection) -> list[EventRecord]:
            sql = """
                SELECT * FROM events
                WHERE project_id = ?
                ORDER BY timestamp ASC, id ASC
            """
            params: tuple[Any, ...] = (project_id,)
            if limit is not None:
                sql += " LIMIT ?"
                params = (project_id, limit)
            rows = conn.execute(sql, params).fetchall()
            return [_event_from_row(row) for row in rows]

        return await self._db.run(query)


class SqliteContractRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def upsert(self, contract: ModuleContract) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO module_contracts (
                    project_id, path, task_id, rendered, payload, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, path) DO UPDATE SET
                    task_id = excluded.task_id,
                    rendered = excluded.rendered,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    contract.project_id,
                    contract.path,
                    contract.task_id,
                    contract.rendered,
                    _dump_json(contract.payload),
                    _dt_to_str(contract.updated_at),
                ),
            )
            conn.commit()

        await self._db.run(write)

    async def get(self, project_id: str, path: str) -> ModuleContract | None:
        def query(conn: sqlite3.Connection) -> ModuleContract | None:
            row = conn.execute(
                """
                SELECT * FROM module_contracts
                WHERE project_id = ? AND path = ?
                """,
                (project_id, path),
            ).fetchone()
            return _contract_from_row(row) if row is not None else None

        return await self._db.run(query)

    async def list_for_project(self, project_id: str) -> list[ModuleContract]:
        def query(conn: sqlite3.Connection) -> list[ModuleContract]:
            rows = conn.execute(
                """
                SELECT * FROM module_contracts
                WHERE project_id = ?
                ORDER BY path ASC
                """,
                (project_id,),
            ).fetchall()
            return [_contract_from_row(row) for row in rows]

        return await self._db.run(query)


class SqliteMemoryRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def insert_node(self, node: MemoryNode) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO memory_nodes (
                    node_id, project_id, task_id, kind, layer, name, body,
                    fingerprint, asserted_at, retracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.node_id,
                    node.project_id,
                    node.task_id,
                    node.kind.value,
                    node.layer.value,
                    node.name,
                    node.body,
                    node.fingerprint,
                    _dt_to_str(node.asserted_at),
                    _dt_to_str(node.retracted_at) if node.retracted_at else None,
                ),
            )
            conn.commit()

        await self._db.run(write)

    async def update_node(self, node: MemoryNode) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE memory_nodes SET
                    project_id = ?, task_id = ?, kind = ?, layer = ?, name = ?,
                    body = ?, fingerprint = ?, asserted_at = ?, retracted_at = ?
                WHERE node_id = ?
                """,
                (
                    node.project_id,
                    node.task_id,
                    node.kind.value,
                    node.layer.value,
                    node.name,
                    node.body,
                    node.fingerprint,
                    _dt_to_str(node.asserted_at),
                    _dt_to_str(node.retracted_at) if node.retracted_at else None,
                    node.node_id,
                ),
            )
            conn.commit()

        await self._db.run(write)

    async def get_node(self, node_id: str) -> MemoryNode | None:
        def query(conn: sqlite3.Connection) -> MemoryNode | None:
            row = conn.execute(
                "SELECT * FROM memory_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            return _memory_node_from_row(row) if row is not None else None

        return await self._db.run(query)

    async def list_nodes(
        self, project_id: str, *, task_id: str | None = None, live_only: bool = True
    ) -> list[MemoryNode]:
        def query(conn: sqlite3.Connection) -> list[MemoryNode]:
            sql = "SELECT * FROM memory_nodes WHERE project_id = ?"
            params: list[Any] = [project_id]
            if task_id is not None:
                sql += " AND task_id = ?"
                params.append(task_id)
            if live_only:
                sql += " AND retracted_at IS NULL"
            sql += " ORDER BY asserted_at ASC, node_id ASC"
            rows = conn.execute(sql, params).fetchall()
            return [_memory_node_from_row(row) for row in rows]

        return await self._db.run(query)

    async def insert_edge(self, edge: MemoryEdge) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO memory_edges (
                    edge_id, project_id, source_id, target_id, kind, asserted_at, retracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.edge_id,
                    edge.project_id,
                    edge.source_id,
                    edge.target_id,
                    edge.kind.value,
                    _dt_to_str(edge.asserted_at),
                    _dt_to_str(edge.retracted_at) if edge.retracted_at else None,
                ),
            )
            conn.commit()

        await self._db.run(write)

    async def list_edges(self, project_id: str, *, live_only: bool = True) -> list[MemoryEdge]:
        def query(conn: sqlite3.Connection) -> list[MemoryEdge]:
            sql = "SELECT * FROM memory_edges WHERE project_id = ?"
            params: list[Any] = [project_id]
            if live_only:
                sql += " AND retracted_at IS NULL"
            sql += " ORDER BY asserted_at ASC, edge_id ASC"
            rows = conn.execute(sql, params).fetchall()
            return [_memory_edge_from_row(row) for row in rows]

        return await self._db.run(query)

    async def search(self, project_id: str, query: str, *, task_id: str | None = None, limit: int = 20) -> list[MemoryNode]:
        match = _fts_query(query)

        def run(conn: sqlite3.Connection) -> list[MemoryNode]:
            if not match or limit <= 0:
                return []
            rows = conn.execute(
                """
                SELECT n.* FROM memory_nodes AS n
                JOIN memory_search AS s ON s.node_id = n.node_id
                WHERE memory_search MATCH ? AND n.project_id = ? AND n.retracted_at IS NULL
                  AND (? IS NULL OR n.task_id = ?)
                ORDER BY n.asserted_at ASC, n.node_id ASC
                LIMIT ?
                """,
                (match, project_id, task_id, task_id, limit),
            ).fetchall()
            return [_memory_node_from_row(row) for row in rows]

        return await self._db.run(run)


class SqliteStateStore:
    """Durable store. Reconstructs a project by reading tables, not model context."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db = SqliteDatabase(self.path)
        self.projects = SqliteProjectRepository(self._db)
        self.tasks = SqliteTaskRepository(self._db)
        self.checkpoints = SqliteCheckpointRepository(self._db)
        self.attempts = SqliteAttemptRepository(self._db)
        self.events = SqliteEventRepository(self._db)
        self.contracts = SqliteContractRepository(self._db)
        self.memory = SqliteMemoryRepository(self._db)

    async def delete_project(self, project_id: str) -> None:
        def delete(conn):
            with conn:
                for table in (
                    'memory_edges',
                    'memory_nodes',
                    'task_attempts',
                    'checkpoints',
                    'module_contracts',
                    'events',
                    'tasks',
                    'projects',
                ):
                    conn.execute(f'DELETE FROM {table} WHERE project_id=?',(project_id,))
        await self._db.run(delete)

    async def initialize(self) -> None:
        await self._db.initialize()

    async def close(self) -> None:
        await self._db.close()

    async def load_snapshot(self, project_id: str) -> ProjectSnapshot | None:
        project = await self.projects.get(project_id)
        if project is None:
            return None
        checkpoints = await self.checkpoints.list(project_id)
        return ProjectSnapshot(
            project=project,
            tasks=await self.tasks.list_for_project(project_id),
            attempts=await self.attempts.list_for_project(project_id),
            checkpoints=checkpoints,
            latest_checkpoint=checkpoints[-1] if checkpoints else None,
        )

    async def __aenter__(self) -> SqliteStateStore:
        await self.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

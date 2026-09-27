"""Schema versions applied to a SQLite connection."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    state TEXT NOT NULL,
    paused_from TEXT,
    architecture_summary TEXT,
    directory_structure TEXT NOT NULL DEFAULT '[]',
    documentation_requirements TEXT NOT NULL DEFAULT '[]',
    current_task_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    target_files TEXT NOT NULL DEFAULT '[]',
    required_context TEXT NOT NULL DEFAULT '[]',
    dependencies TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    test_files TEXT NOT NULL DEFAULT '[]',
    test_commands TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT,
    PRIMARY KEY (project_id, task_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS task_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    worker_response TEXT,
    test_exit_code INTEGER,
    test_log_path TEXT,
    result TEXT,
    UNIQUE (project_id, task_id, attempt_number),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    component TEXT NOT NULL,
    event TEXT NOT NULL,
    attempt INTEGER,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_project_created
    ON checkpoints (project_id, created_at, checkpoint_id);

CREATE INDEX IF NOT EXISTS idx_attempts_project_task
    ON task_attempts (project_id, task_id, attempt_number);

CREATE INDEX IF NOT EXISTS idx_events_project_timestamp
    ON events (project_id, timestamp);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS module_contracts (
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    task_id TEXT NOT NULL,
    rendered TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, path),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_module_contracts_project
    ON module_contracts (project_id, path);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS memory_nodes (
    node_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    layer TEXT NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    fingerprint TEXT,
    asserted_at TEXT NOT NULL,
    retracted_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS memory_edges (
    edge_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    asserted_at TEXT NOT NULL,
    retracted_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (source_id) REFERENCES memory_nodes(node_id),
    FOREIGN KEY (target_id) REFERENCES memory_nodes(node_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_nodes_live
    ON memory_nodes (project_id, task_id, retracted_at);

CREATE INDEX IF NOT EXISTS idx_memory_edges_project
    ON memory_edges (project_id, kind);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(
    node_id UNINDEXED,
    name,
    body,
    content='memory_nodes',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memory_nodes_ai AFTER INSERT ON memory_nodes BEGIN
    INSERT INTO memory_search(rowid, node_id, name, body)
    VALUES (new.rowid, new.node_id, new.name, new.body);
END;

CREATE TRIGGER IF NOT EXISTS memory_nodes_ad AFTER DELETE ON memory_nodes BEGIN
    INSERT INTO memory_search(memory_search, rowid, node_id, name, body)
    VALUES ('delete', old.rowid, old.node_id, old.name, old.body);
END;

CREATE TRIGGER IF NOT EXISTS memory_nodes_au AFTER UPDATE ON memory_nodes BEGIN
    INSERT INTO memory_search(memory_search, rowid, node_id, name, body)
    VALUES ('delete', old.rowid, old.node_id, old.name, old.body);
    INSERT INTO memory_search(rowid, node_id, name, body)
    VALUES (new.rowid, new.node_id, new.name, new.body);
END;
"""

from mobyai.records.repository import SCHEMA as SCHEMA_V4

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4))


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row[0]
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        connection.executescript(sql)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
    connection.commit()

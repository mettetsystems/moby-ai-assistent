"""SQLite-backed task store. No framework required."""

from __future__ import annotations

import sqlite3


class TaskStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id INTEGER PRIMARY KEY, "
            "title TEXT NOT NULL, "
            "done INTEGER NOT NULL DEFAULT 0)"
        )
        self._conn.commit()

    def create(self, title: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO tasks (title, done) VALUES (?, 0)",
            (title,),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT id, title, done FROM tasks ORDER BY id"
        ).fetchall()
        return [
            {"id": row[0], "title": row[1], "done": bool(row[2])} for row in rows
        ]

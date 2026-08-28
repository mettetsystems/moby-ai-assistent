"""Minimal REST-shaped task API over the SQLite store."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from db import TaskStore  # noqa: E402


class TaskAPI:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.store = TaskStore(db_path)

    def handle(self, method: str, path: str, body: str = "") -> tuple[int, str]:
        if method == "GET" and path == "/tasks":
            return 200, json.dumps(self.store.list())
        if method == "POST" and path == "/tasks":
            payload = json.loads(body or "{}")
            task_id = self.store.create(str(payload.get("title") or "untitled"))
            return 201, json.dumps({"id": task_id})
        return 404, json.dumps({"error": "not found"})

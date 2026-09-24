"""Structural recall. Exact listing plus FTS, scoped to one task when asked."""

from __future__ import annotations

from mobyai.models.memory import MemoryNode
from mobyai.state.repository import StateStore


async def recall(
    store: StateStore,
    project_id: str,
    query: str,
    *,
    task_id: str | None = None,
    limit: int = 10,
) -> list[MemoryNode]:
    return await store.memory.search(project_id, query, task_id=task_id, limit=limit)

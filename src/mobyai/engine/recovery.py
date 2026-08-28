"""Reconstruct execution state from persistence. Models are not consulted."""

from __future__ import annotations

from mobyai.models.snapshot import ProjectSnapshot
from mobyai.state.repository import StateStore


class ProjectNotFoundError(LookupError):
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"Project not found: {project_id}")


async def resume_project(store: StateStore, project_id: str) -> ProjectSnapshot:
    """Load the last persisted snapshot so a harness can continue after restart."""
    snapshot = await store.load_snapshot(project_id)
    if snapshot is None:
        raise ProjectNotFoundError(project_id)
    return snapshot

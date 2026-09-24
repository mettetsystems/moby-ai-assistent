"""In-memory repositories for tests. Not durable across processes."""

from __future__ import annotations

from collections.abc import Sequence
import re

from mobyai.models.attempt import TaskAttempt
from mobyai.models.checkpoint import Checkpoint
from mobyai.models.contract import ModuleContract
from mobyai.models.event import EventRecord
from mobyai.models.memory import MemoryEdge, MemoryNode
from mobyai.models.project import Project
from mobyai.models.snapshot import ProjectSnapshot
from mobyai.models.task import Task


class InMemoryProjectRepository:
    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}

    async def get(self, project_id: str) -> Project | None:
        project = self._projects.get(project_id)
        return project.model_copy(deep=True) if project is not None else None

    async def save(self, project: Project) -> None:
        self._projects[project.project_id] = project.model_copy(deep=True)

    async def list_ids(self) -> list[str]:
        return sorted(self._projects)


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Task]] = {}

    async def get(self, project_id: str, task_id: str) -> Task | None:
        task = self._tasks.get(project_id, {}).get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    async def list_for_project(self, project_id: str) -> list[Task]:
        bucket = self._tasks.get(project_id, {})
        return [bucket[task_id].model_copy(deep=True) for task_id in sorted(bucket)]

    async def save(self, project_id: str, task: Task) -> None:
        self._tasks.setdefault(project_id, {})[task.task_id] = task.model_copy(deep=True)

    async def save_all(self, project_id: str, tasks: Sequence[Task]) -> None:
        bucket = self._tasks.setdefault(project_id, {})
        for task in tasks:
            bucket[task.task_id] = task.model_copy(deep=True)


class InMemoryCheckpointRepository:
    def __init__(self) -> None:
        self._checkpoints: dict[str, list[Checkpoint]] = {}

    async def save(self, checkpoint: Checkpoint) -> None:
        bucket = self._checkpoints.setdefault(checkpoint.project_id, [])
        stored = checkpoint.model_copy(deep=True)
        for index, existing in enumerate(bucket):
            if existing.checkpoint_id == stored.checkpoint_id:
                bucket[index] = stored
                return
        bucket.append(stored)

    async def latest(self, project_id: str) -> Checkpoint | None:
        bucket = self._checkpoints.get(project_id, [])
        if not bucket:
            return None
        newest = max(bucket, key=lambda item: (item.created_at, item.checkpoint_id))
        return newest.model_copy(deep=True)

    async def list(self, project_id: str) -> list[Checkpoint]:
        bucket = self._checkpoints.get(project_id, [])
        ordered = sorted(bucket, key=lambda item: (item.created_at, item.checkpoint_id))
        return [item.model_copy(deep=True) for item in ordered]


class InMemoryAttemptRepository:
    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str, int], TaskAttempt] = {}
        self._next_id = 1

    async def save(self, attempt: TaskAttempt) -> TaskAttempt:
        key = (attempt.project_id, attempt.task_id, attempt.attempt_number)
        existing = self._attempts.get(key)
        stored_id = existing.id if existing is not None else self._next_id
        if existing is None:
            self._next_id += 1
        stored = attempt.model_copy(deep=True, update={"id": stored_id})
        self._attempts[key] = stored
        return stored.model_copy(deep=True)

    async def get(
        self, project_id: str, task_id: str, attempt_number: int
    ) -> TaskAttempt | None:
        attempt = self._attempts.get((project_id, task_id, attempt_number))
        return attempt.model_copy(deep=True) if attempt is not None else None

    async def list_for_task(self, project_id: str, task_id: str) -> list[TaskAttempt]:
        matches = [
            attempt
            for (pid, tid, _num), attempt in self._attempts.items()
            if pid == project_id and tid == task_id
        ]
        matches.sort(key=lambda item: item.attempt_number)
        return [item.model_copy(deep=True) for item in matches]

    async def list_for_project(self, project_id: str) -> list[TaskAttempt]:
        matches = [
            attempt
            for (pid, _tid, _num), attempt in self._attempts.items()
            if pid == project_id
        ]
        matches.sort(key=lambda item: (item.task_id, item.attempt_number))
        return [item.model_copy(deep=True) for item in matches]


class InMemoryEventRepository:
    def __init__(self) -> None:
        self._events: list[EventRecord] = []
        self._next_id = 1

    async def append(self, event: EventRecord) -> EventRecord:
        stored = event.model_copy(deep=True, update={"id": self._next_id})
        self._next_id += 1
        self._events.append(stored)
        return stored.model_copy(deep=True)

    async def list_for_project(
        self, project_id: str, *, limit: int | None = None
    ) -> list[EventRecord]:
        matches = [
            event
            for event in self._events
            if event.project_id == project_id
        ]
        matches.sort(key=lambda item: (item.timestamp, item.id or 0))
        if limit is not None:
            matches = matches[:limit]
        return [item.model_copy(deep=True) for item in matches]


class InMemoryContractRepository:
    def __init__(self) -> None:
        self._contracts: dict[tuple[str, str], ModuleContract] = {}

    async def upsert(self, contract: ModuleContract) -> None:
        self._contracts[(contract.project_id, contract.path)] = contract.model_copy(
            deep=True
        )

    async def get(self, project_id: str, path: str) -> ModuleContract | None:
        item = self._contracts.get((project_id, path))
        return item.model_copy(deep=True) if item is not None else None

    async def list_for_project(self, project_id: str) -> list[ModuleContract]:
        matches = [
            item
            for (pid, _path), item in self._contracts.items()
            if pid == project_id
        ]
        matches.sort(key=lambda item: item.path)
        return [item.model_copy(deep=True) for item in matches]


class InMemoryMemoryRepository:
    def __init__(self) -> None:
        self._nodes: dict[str, MemoryNode] = {}
        self._edges: dict[str, MemoryEdge] = {}

    async def insert_node(self, node: MemoryNode) -> None:
        self._nodes[node.node_id] = node.model_copy(deep=True)

    async def update_node(self, node: MemoryNode) -> None:
        self._nodes[node.node_id] = node.model_copy(deep=True)

    async def get_node(self, node_id: str) -> MemoryNode | None:
        item = self._nodes.get(node_id)
        return item.model_copy(deep=True) if item is not None else None

    async def list_nodes(
        self, project_id: str, *, task_id: str | None = None, live_only: bool = True
    ) -> list[MemoryNode]:
        matches = [
            item
            for item in self._nodes.values()
            if item.project_id == project_id
            and (task_id is None or item.task_id == task_id)
            and (not live_only or item.retracted_at is None)
        ]
        matches.sort(key=lambda item: (item.asserted_at.isoformat(), item.node_id))
        return [item.model_copy(deep=True) for item in matches]

    async def insert_edge(self, edge: MemoryEdge) -> None:
        self._edges[edge.edge_id] = edge.model_copy(deep=True)

    async def list_edges(self, project_id: str, *, live_only: bool = True) -> list[MemoryEdge]:
        matches = [
            item
            for item in self._edges.values()
            if item.project_id == project_id and (not live_only or item.retracted_at is None)
        ]
        matches.sort(key=lambda item: (item.asserted_at.isoformat(), item.edge_id))
        return [item.model_copy(deep=True) for item in matches]

    async def search(self, project_id: str, query: str, *, task_id: str | None = None, limit: int = 20) -> list[MemoryNode]:
        terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query)]
        if not terms or limit <= 0:
            return []
        matches = []
        for item in self._nodes.values():
            if item.project_id != project_id or item.retracted_at is not None:
                continue
            if task_id is not None and item.task_id != task_id:
                continue
            haystack = f"{item.name} {item.body}".lower()
            if any(term in haystack for term in terms):
                matches.append(item)
        matches.sort(key=lambda item: (item.asserted_at.isoformat(), item.node_id))
        return [item.model_copy(deep=True) for item in matches[:limit]]


class InMemoryStateStore:
    """Convenience facade used by tests that should not touch disk."""

    def __init__(self) -> None:
        self.projects = InMemoryProjectRepository()
        self.tasks = InMemoryTaskRepository()
        self.checkpoints = InMemoryCheckpointRepository()
        self.attempts = InMemoryAttemptRepository()
        self.events = InMemoryEventRepository()
        self.contracts = InMemoryContractRepository()
        self.memory = InMemoryMemoryRepository()

    async def delete_project(self, project_id: str) -> None:
        self.projects._projects.pop(project_id,None)
        self.tasks._tasks.pop(project_id,None)
        self.checkpoints._checkpoints.pop(project_id,None)
        self.attempts._attempts = {key:value for key,value in self.attempts._attempts.items() if key[0]!=project_id}
        self.contracts._contracts = {key:value for key,value in self.contracts._contracts.items() if key[0]!=project_id}
        self.events._events = [event for event in self.events._events if event.project_id!=project_id]
        self.memory._nodes = {key:value for key,value in self.memory._nodes.items() if value.project_id!=project_id}
        self.memory._edges = {key:value for key,value in self.memory._edges.items() if value.project_id!=project_id}

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

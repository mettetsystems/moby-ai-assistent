"""Write rules for the memory graph. Invalidate; do not delete."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from mobyai.models.memory import MemoryEdge, MemoryEdgeKind, MemoryKind, MemoryLayer, MemoryNode
from mobyai.state.repository import StateStore


def _new_id() -> str:
    return uuid4().hex


def normalized(text: str) -> str:
    return " ".join(text.lower().split())


async def _live(
    store: StateStore, project_id: str, task_id: str, kind: MemoryKind
) -> list[MemoryNode]:
    nodes = await store.memory.list_nodes(project_id, task_id=task_id, live_only=True)
    return [node for node in nodes if node.kind is kind]


async def _edge(
    store: StateStore,
    *,
    project_id: str,
    source_id: str,
    target_id: str,
    kind: MemoryEdgeKind,
    now: datetime,
) -> None:
    await store.memory.insert_edge(
        MemoryEdge(
            edge_id=_new_id(),
            project_id=project_id,
            source_id=source_id,
            target_id=target_id,
            kind=kind,
            asserted_at=now,
        )
    )


async def _retract(store: StateStore, node: MemoryNode, now: datetime) -> MemoryNode:
    retracted = node.model_copy(update={"retracted_at": now})
    await store.memory.update_node(retracted)
    return retracted


async def record_handoff(
    store: StateStore,
    *,
    project_id: str,
    task_id: str,
    body: str,
    now: datetime,
) -> MemoryNode:
    """One live handoff per task. The previous holder is retracted and superseded."""
    previous = await _live(store, project_id, task_id, MemoryKind.HANDOFF)
    node = MemoryNode(
        node_id=_new_id(),
        project_id=project_id,
        task_id=task_id,
        kind=MemoryKind.HANDOFF,
        layer=MemoryLayer.HOT,
        name=f"handoff:{task_id}",
        body=body,
        asserted_at=now,
    )
    await store.memory.insert_node(node)
    for older in previous:
        await _retract(store, older, now)
        await _edge(
            store,
            project_id=project_id,
            source_id=node.node_id,
            target_id=older.node_id,
            kind=MemoryEdgeKind.SUPERSEDES,
            now=now,
        )
    return node


async def record_hypothesis(
    store: StateStore,
    *,
    project_id: str,
    task_id: str,
    body: str,
    fingerprint: str,
    now: datetime,
) -> MemoryNode:
    """A repeated failure fingerprint supersedes the prior hypothesis."""
    previous = [
        node
        for node in await _live(store, project_id, task_id, MemoryKind.HYPOTHESIS)
        if node.fingerprint == fingerprint
    ]
    node = MemoryNode(
        node_id=_new_id(),
        project_id=project_id,
        task_id=task_id,
        kind=MemoryKind.HYPOTHESIS,
        layer=MemoryLayer.HOT,
        name=f"hypothesis:{fingerprint[:12]}",
        body=body,
        fingerprint=fingerprint,
        asserted_at=now,
    )
    await store.memory.insert_node(node)
    for older in previous:
        await _retract(store, older, now)
        await _edge(
            store,
            project_id=project_id,
            source_id=node.node_id,
            target_id=older.node_id,
            kind=MemoryEdgeKind.SUPERSEDES,
            now=now,
        )
    return node


async def record_claim(
    store: StateStore,
    *,
    project_id: str,
    task_id: str,
    name: str,
    body: str,
    now: datetime,
) -> MemoryNode:
    """Identical open claims are reused. A different body stays visible and contradicts."""
    previous = [
        node
        for node in await _live(store, project_id, task_id, MemoryKind.CLAIM)
        if node.name == name
    ]
    same = next((node for node in previous if normalized(node.body) == normalized(body)), None)
    if same is not None:
        return same
    node = MemoryNode(
        node_id=_new_id(),
        project_id=project_id,
        task_id=task_id,
        kind=MemoryKind.CLAIM,
        layer=MemoryLayer.HOT,
        name=name,
        body=body,
        asserted_at=now,
    )
    await store.memory.insert_node(node)
    for older in previous:
        await _edge(
            store,
            project_id=project_id,
            source_id=node.node_id,
            target_id=older.node_id,
            kind=MemoryEdgeKind.CONTRADICTS,
            now=now,
        )
    return node


async def record_artifact_ref(
    store: StateStore,
    *,
    project_id: str,
    task_id: str,
    name: str,
    body: str,
    now: datetime,
) -> MemoryNode:
    """Pointer to an existing log, attempt, or contract. Does not copy source or chat."""
    previous = [
        node
        for node in await _live(store, project_id, task_id, MemoryKind.ARTIFACT_REF)
        if node.name == name
    ]
    same = next((node for node in previous if node.body == body), None)
    if same is not None:
        return same
    node = MemoryNode(
        node_id=_new_id(),
        project_id=project_id,
        task_id=task_id,
        kind=MemoryKind.ARTIFACT_REF,
        layer=MemoryLayer.WARM,
        name=name,
        body=body,
        asserted_at=now,
    )
    await store.memory.insert_node(node)
    for older in previous:
        await _retract(store, older, now)
        await _edge(
            store,
            project_id=project_id,
            source_id=node.node_id,
            target_id=older.node_id,
            kind=MemoryEdgeKind.SUPERSEDES,
            now=now,
        )
    return node


async def link_derived(
    store: StateStore, *, project_id: str, source_id: str, target_id: str, now: datetime
) -> None:
    await _edge(
        store,
        project_id=project_id,
        source_id=source_id,
        target_id=target_id,
        kind=MemoryEdgeKind.DERIVED_FROM,
        now=now,
    )


async def settle_success(
    store: StateStore,
    *,
    project_id: str,
    task_id: str,
    now: datetime,
    success_summary: str | None = None,
) -> MemoryNode | None:
    """Record verified success, retaining the prior handoff only as provenance."""
    handoffs = await _live(store, project_id, task_id, MemoryKind.HANDOFF)
    hypotheses = await _live(store, project_id, task_id, MemoryKind.HYPOTHESIS)
    for hypothesis in hypotheses:
        await store.memory.update_node(hypothesis.model_copy(update={"layer": MemoryLayer.COLD}))
    body = success_summary or f"{task_id} passed harness validation."
    previous_outcomes = await _live(store, project_id, task_id, MemoryKind.OUTCOME)
    outcome = MemoryNode(
        node_id=_new_id(),
        project_id=project_id,
        task_id=task_id,
        kind=MemoryKind.OUTCOME,
        layer=MemoryLayer.CORE,
        name=f"outcome:{task_id}",
        body=body,
        asserted_at=now,
    )
    await store.memory.insert_node(outcome)
    for handoff in handoffs:
        await _retract(store, handoff, now)
        await link_derived(
            store,
            project_id=project_id,
            source_id=outcome.node_id,
            target_id=handoff.node_id,
            now=now,
        )
    for older in previous_outcomes:
        await _retract(store, older, now)
        await _edge(
            store,
            project_id=project_id,
            source_id=outcome.node_id,
            target_id=older.node_id,
            kind=MemoryEdgeKind.SUPERSEDES,
            now=now,
        )
    return outcome

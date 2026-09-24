"""Deterministic working-set render. Cold nodes stay on disk."""

from __future__ import annotations

from mobyai.context.tokenizer import estimate_tokens, truncate_to_tokens
from mobyai.models.memory import MemoryKind, MemoryLayer, MemoryNode
from mobyai.models.task import TaskStatus
from mobyai.state.repository import StateStore

DEFAULT_MEMORY_TOKEN_BUDGET = 2000
DEFAULT_NODE_CAP = 400

_LAYER_RANK = {MemoryLayer.CORE: 0, MemoryLayer.HOT: 1, MemoryLayer.WARM: 2}
_KIND_RANK = {
    MemoryKind.OUTCOME: 0,
    MemoryKind.HANDOFF: 1,
    MemoryKind.CLAIM: 2,
    MemoryKind.HYPOTHESIS: 3,
    MemoryKind.ARTIFACT_REF: 4,
}


def select_nodes(
    nodes: list[MemoryNode],
    *,
    task_id: str | None = None,
    failure_signature: str | None = None,
) -> list[MemoryNode]:
    chosen: list[MemoryNode] = []
    for node in nodes:
        if task_id is not None and node.task_id != task_id:
            continue
        if node.retracted_at is not None or node.layer is MemoryLayer.COLD:
            continue
        if (
            node.kind is MemoryKind.HYPOTHESIS
            and failure_signature
            and node.fingerprint
            and node.fingerprint != failure_signature
        ):
            continue
        chosen.append(node)
    chosen.sort(
        key=lambda node: (
            _LAYER_RANK[node.layer],
            _KIND_RANK[node.kind],
            node.asserted_at.isoformat(),
            node.node_id,
        )
    )
    return chosen


def _render_node(node: MemoryNode, *, node_cap: int) -> str:
    if node.layer is MemoryLayer.CORE:
        body, truncated = truncate_to_tokens(node.body, node_cap)
        suffix = " [truncated]" if truncated else ""
        return f"- {node.kind.value} {node.name}{suffix}\n{body}"
    if node.layer is MemoryLayer.HOT:
        line = " ".join(node.body.split())
        if len(line) > 240:
            line = line[:237] + "..."
        return f"- {node.kind.value} {node.name}: {line}"
    return f"- {node.kind.value} {node.name}"


def assemble_working_set(
    nodes: list[MemoryNode],
    *,
    task_id: str | None = None,
    token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET,
    node_cap: int = DEFAULT_NODE_CAP,
    failure_signature: str | None = None,
    heading: str = "## Shared memory",
) -> str:
    """Render whole nodes within budget, including a bounded omission notice."""
    selected = select_nodes(nodes, task_id=task_id, failure_signature=failure_signature)
    if not selected or token_budget < 1:
        return ""
    if estimate_tokens(heading) > token_budget:
        return ""
    sections = [heading]
    omitted: list[str] = []
    for node in selected:
        block = _render_node(node, node_cap=node_cap)
        if estimate_tokens("\n".join([*sections, block])) > token_budget:
            omitted.append(node.name)
            continue
        sections.append(block)
    if omitted:
        notices = [
            "Omitted from shared memory: " + ", ".join(omitted),
            f"Omitted from shared memory: {len(omitted)} nodes",
            f"Omitted: {len(omitted)}",
        ]
        for notice in notices:
            if estimate_tokens("\n".join([*sections, notice])) <= token_budget:
                sections.append(notice)
                break
    return "\n".join(sections)


async def working_set_for(
    store: StateStore,
    project_id: str,
    task_id: str,
    *,
    failure_signature: str | None = None,
    token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET,
) -> str:
    if token_budget < 1:
        return ""
    nodes = await store.memory.list_nodes(project_id, task_id=task_id, live_only=True)
    # Recall verified outcomes only from completed direct dependencies. Bound both
    # the number of lookups and their share of the prompt; current-task memory wins.
    dependency_nodes: list[MemoryNode] = []
    task = await store.tasks.get(project_id, task_id)
    for dependency_id in (task.dependencies[:8] if task else []):
        dependency = await store.tasks.get(project_id, dependency_id)
        if dependency is None or dependency.status is not TaskStatus.PASSED:
            continue
        outcomes = [
            node for node in await store.memory.list_nodes(project_id, task_id=dependency_id)
            if node.kind is MemoryKind.OUTCOME and node.layer is MemoryLayer.CORE
        ]
        if outcomes:
            dependency_nodes.append(outcomes[-1])
    dependency_budget = min(500, token_budget // 4) if dependency_nodes else 0
    dependency_block = assemble_working_set(
        dependency_nodes,
        token_budget=dependency_budget,
        node_cap=max(1, dependency_budget // 2),
        heading="## Completed dependency outcomes",
    )
    current_budget = token_budget - estimate_tokens(dependency_block) - (1 if dependency_block else 0)
    current_block = assemble_working_set(
        nodes,
        task_id=task_id,
        token_budget=current_budget,
        failure_signature=failure_signature,
    )
    return "\n".join(block for block in (current_block, dependency_block) if block)

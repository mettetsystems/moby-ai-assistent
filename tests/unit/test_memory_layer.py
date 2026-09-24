from datetime import timedelta
from pathlib import Path

import pytest

from mobyai.context.packer import ContextPacker
from mobyai.context.tokenizer import estimate_tokens
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.workspace import Workspace
from mobyai.memory.assembly import assemble_working_set
from mobyai.memory.graph import record_claim, record_handoff, record_hypothesis
from mobyai.memory.record import record_attempt_memory, record_escalation_memory
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.memory import MemoryEdgeKind, MemoryKind, MemoryLayer, MemoryNode
from mobyai.state.memory import InMemoryStateStore
from mobyai.state.sqlite import SqliteStateStore
from tests.helpers import FROZEN_NOW, make_project, make_task


def _attempt(number: int = 1, log: str = "artifacts/logs/t/attempt_001.log") -> TaskAttempt:
    return TaskAttempt(
        project_id="proj-1",
        task_id="STEP_001",
        attempt_number=number,
        started_at=FROZEN_NOW,
        finished_at=FROZEN_NOW,
        test_log_path=log,
        result=AttemptResult.FAIL,
    )


def _node(
    node_id: str,
    *,
    kind: MemoryKind = MemoryKind.OUTCOME,
    layer: MemoryLayer = MemoryLayer.CORE,
    name: str = "outcome:STEP_001",
    body: str = "passed",
    task_id: str = "STEP_001",
    fingerprint: str | None = None,
    minutes: int = 0,
) -> MemoryNode:
    return MemoryNode(
        node_id=node_id,
        project_id="proj-1",
        task_id=task_id,
        kind=kind,
        layer=layer,
        name=name,
        body=body,
        fingerprint=fingerprint,
        asserted_at=FROZEN_NOW + timedelta(minutes=minutes),
    )


async def test_handoff_slot_supersedes_previous() -> None:
    store = InMemoryStateStore()
    first = await record_handoff(
        store, project_id="proj-1", task_id="STEP_001", body="first", now=FROZEN_NOW
    )
    second = await record_handoff(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        body="second",
        now=FROZEN_NOW + timedelta(minutes=1),
    )
    live = await store.memory.list_nodes("proj-1", task_id="STEP_001")
    assert [node.node_id for node in live if node.kind is MemoryKind.HANDOFF] == [second.node_id]
    edges = await store.memory.list_edges("proj-1")
    assert any(
        edge.kind is MemoryEdgeKind.SUPERSEDES
        and edge.source_id == second.node_id
        and edge.target_id == first.node_id
        for edge in edges
    )
    stored = await store.memory.get_node(first.node_id)
    assert stored is not None and stored.retracted_at is not None


async def test_repeated_hypothesis_supersedes_same_fingerprint() -> None:
    store = InMemoryStateStore()
    first = await record_hypothesis(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        body="try cache",
        fingerprint="abc",
        now=FROZEN_NOW,
    )
    second = await record_hypothesis(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        body="try cache again",
        fingerprint="abc",
        now=FROZEN_NOW + timedelta(minutes=1),
    )
    other = await record_hypothesis(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        body="different failure",
        fingerprint="xyz",
        now=FROZEN_NOW + timedelta(minutes=2),
    )
    live = await store.memory.list_nodes("proj-1", task_id="STEP_001")
    ids = {node.node_id for node in live}
    assert first.node_id not in ids
    assert {second.node_id, other.node_id} <= ids


async def test_conflicting_claim_stays_visible() -> None:
    store = InMemoryStateStore()
    first = await record_claim(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        name="repair:STEP_001",
        body="raise the timeout",
        now=FROZEN_NOW,
    )
    same = await record_claim(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        name="repair:STEP_001",
        body="  raise   the timeout ",
        now=FROZEN_NOW,
    )
    assert same.node_id == first.node_id
    second = await record_claim(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        name="repair:STEP_001",
        body="fix the assertion",
        now=FROZEN_NOW + timedelta(minutes=1),
    )
    live = await store.memory.list_nodes("proj-1", task_id="STEP_001")
    assert {first.node_id, second.node_id} <= {node.node_id for node in live}
    edges = await store.memory.list_edges("proj-1")
    assert any(edge.kind is MemoryEdgeKind.CONTRADICTS and edge.source_id == second.node_id for edge in edges)


async def test_pass_promotes_outcome_and_cools_hypotheses(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    task = make_task("STEP_001", test_commands=["pytest"])
    workspace = Workspace(tmp_path)
    await record_hypothesis(
        store,
        project_id="proj-1",
        task_id="STEP_001",
        body="old guess",
        fingerprint="abc",
        now=FROZEN_NOW,
    )
    await record_handoff(
        store, project_id="proj-1", task_id="STEP_001", body="still failing", now=FROZEN_NOW
    )
    await record_attempt_memory(
        store,
        project_id="proj-1",
        task=task,
        attempt=_attempt().model_copy(update={"result": AttemptResult.PASS}),
        passed=True,
        error="",
        instruction="",
        workspace=workspace,
        now=FROZEN_NOW + timedelta(minutes=1),
    )
    live = await store.memory.list_nodes("proj-1", task_id="STEP_001", live_only=False)
    outcomes = [node for node in live if node.kind is MemoryKind.OUTCOME and node.retracted_at is None]
    assert len(outcomes) == 1
    assert outcomes[0].layer is MemoryLayer.CORE
    assert "passed harness validation on attempt 1" in outcomes[0].body
    assert "still failing" not in outcomes[0].body
    assert "pytest" in outcomes[0].body
    hypotheses = [node for node in live if node.kind is MemoryKind.HYPOTHESIS]
    assert hypotheses and all(node.layer is MemoryLayer.COLD for node in hypotheses)
    assert not any(node.kind is MemoryKind.HANDOFF and node.retracted_at is None for node in live)


async def test_failed_attempt_links_hypothesis_to_log_pointer(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    task = make_task("STEP_001", test_commands=["pytest"], target_files=["src/a.py"])
    await record_attempt_memory(
        store,
        project_id="proj-1",
        task=task,
        attempt=_attempt(),
        passed=False,
        error="assertion failed",
        instruction="check the boundary",
        workspace=Workspace(tmp_path),
        now=FROZEN_NOW,
    )
    await record_attempt_memory(
        store,
        project_id="proj-1",
        task=task,
        attempt=_attempt(2, "artifacts/logs/t/attempt_002.log"),
        passed=False,
        error="assertion failed",
        instruction="check the boundary",
        workspace=Workspace(tmp_path),
        now=FROZEN_NOW + timedelta(minutes=1),
    )
    live = await store.memory.list_nodes("proj-1", task_id="STEP_001")
    hypotheses = [node for node in live if node.kind is MemoryKind.HYPOTHESIS]
    assert len(hypotheses) == 1
    assert "check the boundary" in hypotheses[0].body
    artifacts = [node for node in live if node.kind is MemoryKind.ARTIFACT_REF]
    assert len(artifacts) == 2
    assert all("src/a.py" not in node.body for node in artifacts)
    edges = await store.memory.list_edges("proj-1")
    assert any(
        edge.kind is MemoryEdgeKind.DERIVED_FROM and edge.source_id == hypotheses[0].node_id
        for edge in edges
    )


def test_assembly_is_layered_and_budgeted() -> None:
    nodes = [
        _node("warm", kind=MemoryKind.ARTIFACT_REF, layer=MemoryLayer.WARM, name="artifact:1", body="log", minutes=2),
        _node("hot", kind=MemoryKind.HANDOFF, layer=MemoryLayer.HOT, name="handoff:STEP_001", body="retry the boundary check", minutes=1),
        _node("core", kind=MemoryKind.OUTCOME, layer=MemoryLayer.CORE, name="outcome:STEP_001", body="x" * 4000, minutes=0),
        _node("cold", kind=MemoryKind.HYPOTHESIS, layer=MemoryLayer.COLD, name="hypothesis:old", body="hidden", minutes=3),
        _node("other", task_id="STEP_009", name="outcome:STEP_009", body="other task"),
        _node(
            "other-fail",
            kind=MemoryKind.HYPOTHESIS,
            layer=MemoryLayer.HOT,
            name="hypothesis:xyz",
            body="unrelated failure",
            fingerprint="xyz",
            minutes=4,
        ),
    ]
    rendered = assemble_working_set(
        nodes, task_id="STEP_001", token_budget=80, node_cap=20, failure_signature="abc"
    )
    assert rendered.index("outcome:STEP_001") < rendered.index("handoff:STEP_001")
    assert "hypothesis:old" not in rendered
    assert "unrelated failure" not in rendered
    assert "outcome:STEP_009" not in rendered
    assert estimate_tokens(rendered) <= 80
    tight = assemble_working_set(
        nodes, task_id="STEP_001", token_budget=12, node_cap=8, failure_signature="abc"
    )
    assert estimate_tokens(tight) <= 12


async def test_packer_keeps_acceptance_when_memory_is_large(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    space = Workspace(root)
    files = WorkspaceFilesystem(space)
    store = InMemoryStateStore()
    project = make_project(workspace_root=root)
    task = make_task(
        "STEP_001",
        acceptance_criteria=["EXACT_ACCEPTANCE_RULE"],
        test_commands=["pytest -q"],
        status="ready",
    )
    await store.projects.save(project)
    await store.tasks.save(project.project_id, task)
    await store.memory.insert_node(
        _node("huge", body="MEMORY_BODY_" + ("pad " * 5000), name="outcome:STEP_001")
    )
    context = await ContextPacker(files, store, memory_token_budget=30).build_worker_context(
        project.project_id, "STEP_001", 4000
    )
    rendered = context.render()
    assert "EXACT_ACCEPTANCE_RULE" in rendered
    assert "pytest -q" in rendered
    assert "Omitted from shared memory: outcome:STEP_001" in context.shared_memory
    assert "pad pad pad pad pad" not in rendered
    assert context.token_count < 4000


async def test_worker_and_escalation_share_the_same_task_memory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = InMemoryStateStore()
    project = make_project(workspace_root=root)
    task = make_task("STEP_001", test_commands=["pytest"], status="ready")
    other = make_task("STEP_009")
    await store.projects.save(project)
    await store.tasks.save_all(project.project_id, [task, other])
    await record_attempt_memory(
        store,
        project_id=project.project_id,
        task=task,
        attempt=_attempt(),
        passed=False,
        error="assertion failed",
        instruction="inspect the boundary",
        workspace=Workspace(root),
        now=FROZEN_NOW,
    )
    await store.memory.insert_node(
        _node("foreign", task_id="STEP_009", name="outcome:STEP_009", body="FOREIGN_TASK_FACT")
    )
    context = await ContextPacker(WorkspaceFilesystem(Workspace(root)), store).build_worker_context(
        project.project_id, "STEP_001", 8000
    )
    assert "inspect the boundary" in context.shared_memory
    assert "FOREIGN_TASK_FACT" not in context.shared_memory
    block = await record_escalation_memory(
        store,
        project_id=project.project_id,
        task_id="STEP_001",
        summary="escalate the boundary failure",
        instruction="split the check",
        now=FROZEN_NOW + timedelta(minutes=2),
    )
    assert "split the check" in block
    assert "inspect the boundary" in block
    assert "FOREIGN_TASK_FACT" not in block


async def test_sqlite_memory_round_trip_and_search(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "mobyai.db") as store:
        await store.projects.save(make_project())
        await record_handoff(
            store, project_id="proj-1", task_id="STEP_001", body="token expiry differs", now=FROZEN_NOW
        )
        found = await store.memory.search("proj-1", "expiry")
        assert found and "expiry" in found[0].body
        await store.delete_project("proj-1")
        assert await store.memory.list_nodes("proj-1", live_only=False) == []


@pytest.mark.parametrize("budget", [0, 1, 3, 4, 12, 30, 80, 2000])
def test_omitted_names_cannot_overflow_memory_budget(budget: int) -> None:
    nodes = [
        _node(str(i), name=f"claim:{i}:" + "long_name" * 20, body="x" * 4000)
        for i in range(100)
    ]
    block = assemble_working_set(nodes, token_budget=budget)
    assert estimate_tokens(block) <= budget


async def test_memory_counts_toward_full_worker_budget(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n" * 2000)
    store = InMemoryStateStore()
    project = make_project(workspace_root=tmp_path)
    task = make_task("STEP_001", target_files=["app.py"],
                     acceptance_criteria=["EXACT_RULE"], test_commands=["pytest -q"])
    await store.projects.save(project)
    await store.tasks.save(project.project_id, task)
    await store.memory.insert_node(_node("n", body="Remember the verified boundary."))
    context = await ContextPacker(WorkspaceFilesystem(Workspace(tmp_path)), store).build_worker_context(
        project.project_id, task.task_id, 500
    )
    assert context.shared_memory
    assert context.token_count == estimate_tokens(context.render()) <= 500
    assert "EXACT_RULE" in context.render()
    assert "pytest -q" in context.render()


async def test_memory_yields_to_required_task_evidence(tmp_path: Path) -> None:
    packer = ContextPacker(WorkspaceFilesystem(Workspace(tmp_path)))
    project, task = make_project(), make_task("STEP_001", acceptance_criteria=["EXACT_RULE"])
    base = await packer.pack(project, task)
    context = await packer.pack(project, task, token_budget=base.token_count,
                                shared_memory="memory " * 2000)
    assert context.shared_memory == ""
    assert context.token_count == base.token_count
    assert "EXACT_RULE" in context.render()


@pytest.fixture(params=["memory", "sqlite"])
async def memory_store(request, tmp_path):
    store = InMemoryStateStore() if request.param == "memory" else SqliteStateStore(tmp_path / "memory.db")
    await store.projects.save(make_project())
    try:
        yield store
    finally:
        if isinstance(store, SqliteStateStore):
            await store.close()


async def test_recall_scopes_before_limit_and_excludes_retracted(memory_store) -> None:
    from mobyai.memory.recall import recall

    for i in range(12):
        await memory_store.memory.insert_node(_node(f"other-{i}", task_id="STEP_009", body="expiry"))
    await memory_store.memory.insert_node(_node("old", body="expiry").model_copy(
        update={"retracted_at": FROZEN_NOW}))
    await memory_store.memory.insert_node(_node("wanted", body="expiry", minutes=1))
    found = await recall(memory_store, "proj-1", "expiry", task_id="STEP_001", limit=1)
    assert [node.node_id for node in found] == ["wanted"]
    assert await recall(memory_store, "other-project", "expiry") == []
    assert await recall(memory_store, "proj-1", "expiry", limit=0) == []


@pytest.mark.parametrize("query", ["AND", "NOT", "x OR y", '"AND" (NOT)', "***"])
async def test_search_treats_operators_as_literal_text(memory_store, query: str) -> None:
    await memory_store.memory.insert_node(_node("literal", body="AND NOT x OR y"))
    found = await memory_store.memory.search("proj-1", query)
    assert [node.node_id for node in found] == ([] if query == "***" else ["literal"])


async def test_completed_dependency_recall_is_bounded_and_scoped(memory_store) -> None:
    from mobyai.memory.assembly import working_set_for

    await memory_store.tasks.save_all("proj-1", [
        make_task("STEP_001", dependencies=["STEP_002", "STEP_003"]),
        make_task("STEP_002", status="passed"),
        make_task("STEP_003", status="failed"),
        make_task("STEP_009", status="passed"),
    ])
    for node in [
        _node("current", kind=MemoryKind.HANDOFF, layer=MemoryLayer.HOT, body="CURRENT_GUIDANCE"),
        _node("dependency", task_id="STEP_002", name="outcome:STEP_002", body="VERIFIED_DEPENDENCY"),
        _node("guess", task_id="STEP_002", kind=MemoryKind.CLAIM, body="UNVERIFIED_GUESS"),
        _node("failed", task_id="STEP_003", body="FAILED_TASK_OUTCOME"),
        _node("unrelated", task_id="STEP_009", body="UNRELATED_OUTCOME"),
    ]:
        await memory_store.memory.insert_node(node)
    block = await working_set_for(memory_store, "proj-1", "STEP_001", token_budget=200)
    assert "CURRENT_GUIDANCE" in block
    assert "VERIFIED_DEPENDENCY" in block
    assert "UNVERIFIED_GUESS" not in block
    assert "FAILED_TASK_OUTCOME" not in block
    assert "UNRELATED_OUTCOME" not in block
    assert estimate_tokens(block) <= 200
    for budget in range(1, 60):
        assert estimate_tokens(await working_set_for(
            memory_store, "proj-1", "STEP_001", token_budget=budget)) <= budget


async def test_actual_failures_reach_worker_orchestrator_and_planner(tmp_path, monkeypatch) -> None:
    import json
    from unittest.mock import AsyncMock
    from mobyai.engine.orchestration import advance_project
    from tests.unit.test_orchestrator import (
        _ready_project, _worker, _orchestrator, _empty_payload, _retry_payload, _escalate_payload,
    )

    monkeypatch.setattr("mobyai.research.client.research_lookup", AsyncMock(return_value={"results": []}))
    store, workspace = await _ready_project(tmp_path)
    worker_provider, worker = _worker([json.dumps(_empty_payload())] * 2)
    orch_provider, orchestrator = _orchestrator([
        json.dumps(_retry_payload()), json.dumps(_escalate_payload()),
    ])
    first = await advance_project(await store.projects.get("proj-1"), store, worker, orchestrator, workspace)
    assert "- hypothesis" in orch_provider.requests[0].messages[-1].content
    second = await advance_project(first.project, store, worker, orchestrator, workspace)
    assert any("- hypothesis" in message.content for message in worker_provider.requests[1].messages)
    assert "- hypothesis" in orch_provider.requests[1].messages[-1].content
    assert second.escalation is not None
    assert "- hypothesis" in second.escalation.shared_memory


async def test_memory_survives_sqlite_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    async with SqliteStateStore(path) as store:
        await store.projects.save(make_project())
        await record_handoff(store, project_id="proj-1", task_id="STEP_001",
                             body="Resume this repair", now=FROZEN_NOW)
    async with SqliteStateStore(path) as store:
        from mobyai.memory.assembly import working_set_for
        assert "Resume this repair" in await working_set_for(store, "proj-1", "STEP_001")
        assert len(await store.memory.search("proj-1", "repair")) == 1

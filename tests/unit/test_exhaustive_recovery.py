from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests.unit.test_cli import _runtime
from tests.unit.test_reliability import project
from mobyai.models.project import ProjectState
from mobyai.models.orchestrator import OrchestratorDecision
from mobyai.engine.orchestration import run_project,OrchestrationStep
from mobyai.engine.planner_recovery import recover_planner
from mobyai.execution.workspace import Workspace
from mobyai.execution.diagnostics import diagnose

async def test_more_than_fifty_steps_can_complete(tmp_path,monkeypatch):
    import mobyai.engine.orchestration as engine
    _,runtime=_runtime(tmp_path)
    item=project(tmp_path,ProjectState.READY)
    await runtime.store.projects.save(item)
    calls=[]
    async def advance(current,*args,**kwargs):
        calls.append(1)
        next_project=current.model_copy(update={'state':ProjectState.COMPLETED if len(calls)==60 else ProjectState.READY})
        return OrchestrationStep(project=next_project,decision=OrchestratorDecision.CONTINUE)
    monkeypatch.setattr(engine,'advance_project',advance)
    result=await run_project(item,runtime.store,None,None,Workspace(tmp_path))
    assert len(calls)==60
    assert result.project.state==ProjectState.COMPLETED

async def test_planner_format_failure_is_recoverable_and_context_is_not(tmp_path,monkeypatch):
    _,runtime=_runtime(tmp_path)
    item=project(tmp_path,ProjectState.FAILED)
    await runtime.store.projects.save(item)
    wait=AsyncMock()
    monkeypatch.setattr('mobyai.engine.planner_recovery._backoff',wait)
    result=await recover_planner(item,runtime.store,'Invalid planner JSON',ProjectState.PLANNING,100)
    assert result.state==ProjectState.PLANNING
    wait.assert_awaited_once_with(30)
    paused=await recover_planner(result,runtime.store,'context budget exhausted',ProjectState.PLANNING,101)
    assert paused.state==ProjectState.PAUSED
    assert paused.paused_from==ProjectState.PLANNING
    assert diagnose('Context budget exhausted: required task evidence').category=='context_capacity'

async def test_context_packing_exhaustion_pauses_even_when_tests_also_fail(tmp_path,monkeypatch):
    import json
    from tests.unit.test_orchestrator import _ready_project,_worker,_orchestrator,_retry_payload
    from mobyai.context.errors import ContextBudgetError
    from mobyai.engine.orchestration import advance_project
    store,workspace=await _ready_project(tmp_path)
    monkeypatch.setattr('mobyai.context.packer.ContextPacker.build_worker_context',AsyncMock(side_effect=ContextBudgetError('required evidence cannot fit')))
    _,worker=_worker([])
    _,orchestrator=_orchestrator([json.dumps(_retry_payload())])
    result=await advance_project(await store.projects.get('proj-1'),store,worker,orchestrator,workspace)
    assert result.project.state==ProjectState.PAUSED
    assert result.project.last_error.startswith('Context budget exhausted:')

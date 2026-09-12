import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests.unit.test_orchestrator import _ready_project,_worker,_orchestrator,_empty_payload,_retry_payload,_escalate_payload
from mobyai.engine.orchestration import advance_project
from mobyai.models.project import ProjectState
from mobyai.models.checkpoint import CheckpointKind
from mobyai.research.access import research_access
from mobyai.execution.tools import ToolDispatcher
from mobyai.execution.workspace import Workspace
from mobyai.models.worker import WorkerToolCall

REF={'url':'https://example.org/fix','title':'File creation repair','snippet':'Create the required target file'}

async def test_cache_retry_after_second_failure_and_failed_use_handoff(tmp_path,monkeypatch):
    lookup=AsyncMock(return_value={'status':'ok','results':[REF]})
    monkeypatch.setattr('mobyai.research.client.research_lookup',lookup)
    store,workspace=await _ready_project(tmp_path)
    accepted={**_retry_payload(),'cached_references_sufficient':True}
    provider,orch=_orchestrator([json.dumps(_retry_payload()),json.dumps(accepted),json.dumps(_escalate_payload())])
    _,worker=_worker([json.dumps(_empty_payload()),json.dumps(_empty_payload()),json.dumps({**_empty_payload(),'reference_urls_used':[REF['url']]})])
    first=await advance_project(await store.projects.get('proj-1'),store,worker,orch,workspace)
    lookup.assert_not_called()
    second=await advance_project(first.project,store,worker,orch,workspace)
    assert second.project.state==ProjectState.RETRYING
    assert second.defer_sampling
    lookup.assert_awaited_once()
    checkpoints=await store.checkpoints.list('proj-1')
    instruction=[c.payload['instruction'] for c in checkpoints if c.kind==CheckpointKind.RETRY][-1]
    assert REF['url'] in instruction
    assert 'Repeat unchanged validation' in instruction
    # History survives a new invocation; the cache retry cannot repeat forever.
    third=await advance_project(await store.projects.get('proj-1'),store,worker,orch,workspace)
    assert third.project.state==ProjectState.ESCALATING
    lookup.assert_awaited_once()
    assert '[RESEARCH_REQUIRED]' in third.escalation.decision_summary
    events=await store.events.list_for_project('proj-1')
    failed=[e for e in events if e.event=='cached_repair_failed'][-1]
    assert failed.payload['worker_reported_urls']==[REF['url']]
    assert 'FAILED REFERENCE-ASSISTED ATTEMPT' in provider.requests[-1].messages[-1].content

async def test_model_rejects_irrelevant_cache_without_retry(tmp_path,monkeypatch):
    monkeypatch.setattr('mobyai.research.client.research_lookup',AsyncMock(return_value={'status':'ok','results':[REF]}))
    store,workspace=await _ready_project(tmp_path)
    _,orch=_orchestrator([json.dumps(_retry_payload()),json.dumps(_retry_payload())])
    _,worker=_worker([json.dumps(_empty_payload())]*2)
    first=await advance_project(await store.projects.get('proj-1'),store,worker,orch,workspace)
    second=await advance_project(first.project,store,worker,orch,workspace)
    assert second.project.state==ProjectState.ESCALATING
    assert '[RESEARCH_REQUIRED]' in second.escalation.decision_summary

async def test_network_gate_does_not_call_client_before_authorized_phase(tmp_path,monkeypatch):
    call=AsyncMock(return_value={'status':'ok','results':[]})
    monkeypatch.setattr('mobyai.research.client.research_search',call)
    dispatcher=ToolDispatcher.from_workspace(Workspace(tmp_path))
    request=WorkerToolCall(name='research_search',arguments={'query':'query','error':'error'})
    assert not (await dispatcher.execute(request)).ok
    call.assert_not_called()
    with research_access(True):assert (await dispatcher.execute(request)).ok
    assert not (await dispatcher.execute(request)).ok

@pytest.mark.parametrize('tagged',[False,True])
async def test_planner_internet_gate_requires_research_handoff(tmp_path,tagged):
    from tests.unit.test_replan import _escalated_store,_patch_payload
    from mobyai.engine.replanning import load_escalation_package,replan_project
    from mobyai.models.planner import DAGPatch
    from mobyai.research.access import internet_allowed
    store,workspace=await _escalated_store(tmp_path)
    package=await load_escalation_package(store,'proj-1')
    package=package.model_copy(update={'decision_summary':'[RESEARCH_REQUIRED] Review logs' if tagged else 'Review logs'})
    class Planner:
        async def replan(self,**kwargs):
            assert internet_allowed.get() is tagged
            return DAGPatch.model_validate(_patch_payload())
    await replan_project(await store.projects.get('proj-1'),store,Planner(),workspace=workspace,package=package)
    assert not internet_allowed.get()


def test_hybrid_retrieval_prefers_error_specific_reference(tmp_path):
    from mobyai.research.cache import VectorCache
    cache=VectorCache(tmp_path/'cache.sqlite3')
    cache.put('zenodo','Gradle wrapper ClassNotFoundException',{'source':'zenodo','query':'Gradle wrapper ClassNotFoundException','results':[{'url':'https://example.org/gradle','title':'Gradle wrapper ClassNotFoundException repair','snippet':'Check gradle-wrapper.jar classpath'}]})
    cache.put('zenodo','music matching',{'source':'zenodo','query':'music matching','results':[{'url':'https://example.org/music','title':'Music tempo matching','snippet':'Match heart rate to music tempo'}]})
    result=cache.lookup('Gradle wrapper ClassNotFoundException gradle-wrapper.jar')
    assert result['results'][0]['url']=='https://example.org/gradle'
    assert result['results'][0]['bm25']>0
    assert result['results'][0]['cosine']>0

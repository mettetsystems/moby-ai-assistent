import asyncio
from datetime import datetime, UTC
from io import StringIO
import json
import sys

import httpx
import pytest

from mobyai.cli import async_main, _execute_project
from mobyai.engine.timeouts import TimeoutPolicy, TimeoutApprovalRequired, STEPS, operation_key
from mobyai.execution.commands import CommandRunner
from mobyai.execution.workspace import Workspace
from mobyai.execution.diagnostics import diagnose
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.models.event import EventRecord
from mobyai.models.inference import ChatMessage, ModelRequest
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import make_project
from tests.unit.test_cli_ssh import create


async def test_ladder_persists_and_requires_approval():
    store=InMemoryStateStore()
    policy=await TimeoutPolicy(store,'demo').load()
    for used,expected in zip(STEPS,STEPS[1:]):
        assert policy.budget('command:x',600)==used
        assert await policy.timed_out('command:x',used,'test')==expected
        policy=await TimeoutPolicy(store,'demo').load()
    assert policy.budget('unrelated',600)==600
    with pytest.raises(TimeoutApprovalRequired):
        await policy.timed_out('command:x',1800,'test')
    policy=await TimeoutPolicy(store,'demo').load()
    with pytest.raises(TimeoutApprovalRequired):
        policy.budget('command:x',9000)
    assert policy.budget('new',9000)==1800
    await store.events.append(EventRecord(timestamp=datetime.now(UTC),project_id='demo',component='cli',event='timeout_limit_approved',payload={'seconds':2400}))
    policy=await TimeoutPolicy(store,'demo').load()
    assert policy.budget('command:x',600)==2400
    assert policy.budget('new',600)==600
    with pytest.raises(TimeoutApprovalRequired):
        await policy.timed_out('command:x',2400,'test')


async def test_inference_timeout_increases_without_changing_request():
    budgets=[];payloads=[]
    def handler(request):
        budgets.append(request.extensions['timeout']['read'])
        payloads.append(json.loads(request.content))
        if len(budgets)<3:
            raise httpx.ReadTimeout('slow')
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client)
        policy=await TimeoutPolicy(InMemoryStateStore(),'demo').load()
        with policy.activate():
            await provider.generate(ModelRequest(messages=[ChatMessage(role='user',content='do work')]))
    assert budgets==[600,750,900]
    assert payloads[0]==payloads[1]==payloads[2]


async def test_inference_ceiling_is_not_swallowed_by_transport_retries():
    budgets=[]
    def handler(request):
        budgets.append(request.extensions['timeout']['read'])
        raise httpx.ReadTimeout('slow')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client)
        policy=await TimeoutPolicy(InMemoryStateStore(),'demo').load()
        with policy.activate(),pytest.raises(TimeoutApprovalRequired):
            await provider.generate(ModelRequest(messages=[ChatMessage(role='user',content='do work')]))
    assert budgets==list(STEPS)


async def test_command_records_next_budget_without_replaying(tmp_path,monkeypatch):
    original=asyncio.wait_for
    async def timeout_once(awaitable,timeout):
        if timeout==600:
            awaitable.close()
            raise TimeoutError()
        return await original(awaitable,timeout)
    monkeypatch.setattr('mobyai.execution.commands.asyncio.wait_for',timeout_once)
    policy=await TimeoutPolicy(InMemoryStateStore(),'demo').load()
    command=[sys.executable,'-c','import time; time.sleep(30)']
    with policy.activate():
        result=await CommandRunner(Workspace(tmp_path)).run(command)
    assert result.timed_out
    assert 'Next attempt permits 750 seconds' in result.stderr
    assert len(policy.history)==1
    assert policy.budget(operation_key('command',[str(tmp_path),command]),600)==750
    assert diagnose(result.stderr).category=='timeout'
    assert not diagnose(result.stderr).sampling_eligible


async def test_ceiling_pauses_cli_and_explicit_approval_is_required(tmp_path,monkeypatch):
    runtime,project=await create(tmp_path)
    runtime.config.runtime.keep_awake=False
    async def exhausted(*a,**kw):
        raise TimeoutApprovalRequired('Timeout approval required: operation exhausted 1800 seconds')
    monkeypatch.setattr('mobyai.cli._execute_project_inner',exhausted)
    output=StringIO()
    assert await _execute_project(runtime,project,output,max_steps=None)==0
    saved=await runtime.store.projects.get(project.project_id)
    assert saved.state.value=='PAUSED'
    assert 'Timeout approval required' in saved.last_error
    for seconds,confirmation in [(2400,'wrong'),(1800,project.project_id)]:
        assert await async_main(['approve-timeout',project.project_id,'--seconds',str(seconds),'--confirm',confirmation],runtime=runtime,stderr=StringIO())==1
    assert await async_main(['approve-timeout',project.project_id,'--seconds','2400','--confirm',project.project_id],runtime=runtime,stdout=output)==0
    policy=await TimeoutPolicy(runtime.store,project.project_id).load()
    assert policy.ceiling==2400
    assert (await runtime.store.projects.get(project.project_id)).state.value=='PAUSED'


async def test_planner_receives_timeout_history(tmp_path):
    from tests.unit.test_replan import _escalated_store,_planner,_patch_payload
    from mobyai.engine.replanning import replan_project
    store,workspace=await _escalated_store(tmp_path)
    provider,agent=_planner([json.dumps(_patch_payload())])
    policy=await TimeoutPolicy(store,'proj-1').load()
    await policy.timed_out('command:opaque',600,'command: pytest')
    with policy.activate():
        await replan_project(await store.projects.get('proj-1'),store,agent,workspace=workspace)
    text='\n'.join(m.content for m in provider.requests[0].messages)
    assert 'command:opaque' in text
    assert '750' in text
    assert 'independently validated units' in text


async def test_ui_approval_shares_policy_and_rejects_active_or_invalid_requests(tmp_path):
    from mobyai.console.workloads import Workloads
    from mobyai.engine.lease import project_lease
    from mobyai.engine.timeouts import approval_view
    runtime,project=await create(tmp_path)
    workloads=Workloads(runtime,tmp_path/'config.yaml')
    payload={'project_id':project.project_id,'seconds':2400,'confirmation':project.project_id}
    for invalid in [True,1800,2400.5,'2400',None]:
        with pytest.raises(ValueError):
            await workloads.approve_timeout({**payload,'seconds':invalid})
    with pytest.raises(ValueError):
        await workloads.approve_timeout({**payload,'confirmation':'wrong'})
    with project_lease(runtime.config.persistence.database_path,project.project_id):
        with pytest.raises(ValueError):
            await workloads.approve_timeout(payload)
    result=await workloads.approve_timeout(payload)
    assert result['resumed'] is False
    events=await runtime.store.events.list_for_project(project.project_id)
    assert approval_view(events)['ceiling_seconds']==2400
    assert events[-1].payload['approved_by']=='operator_ui'
    assert (await runtime.store.projects.get(project.project_id)).state==project.state

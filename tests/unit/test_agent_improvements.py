import asyncio
from datetime import datetime, UTC
from hashlib import sha256
import json

import httpx
import pytest

from mobyai.agents.prompts import worker,planner
from mobyai.context.history import compact_history
from mobyai.context.packer import ContextPacker
from mobyai.engine.recovery_evidence import evidence_for,record_decision
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.workspace import Workspace
from mobyai.inference.base import InferenceError
from mobyai.inference.fake import FakeInferenceProvider
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.config import RoleModelConfig
from mobyai.models.inference import ChatMessage,ModelRequest
from mobyai.models.orchestrator import OrchestratorResult,NextPayload,TargetAgent,OrchestratorDecision
from mobyai.models.worker import WorkerResult
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import make_project,make_task


def request():
    return ModelRequest(messages=[ChatMessage(role='user',content='work')])


async def test_schema_opt_in_retains_local_validation():
    payloads=[]
    def handler(req):
        payloads.append(json.loads(req.content))
        content='[]' if len(payloads)==1 else '{"summary":"done","done":true}'
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client)
        config=RoleModelConfig(model='model',endpoint='http://local',structured_output='json_schema')
        result=await StructuredGenerator(provider,settings=config).generate(request(),WorkerResult)
    assert result.done
    assert len(payloads)==2
    assert payloads[0]['response_format']['json_schema']['schema']['type']=='object'
    assert payloads[0]['response_format']==payloads[1]['response_format']


async def test_unsupported_schema_is_actionable_and_not_silently_downgraded():
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(400,text='unsupported schema')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client)
        with pytest.raises(InferenceError,match='structured_output to json'):
            await provider.generate(request().model_copy(update={'response_schema':WorkerResult.model_json_schema()}))
    assert len(calls)==1


class SSE(httpx.AsyncByteStream):
    def __init__(self,chunks): self.chunks=chunks;self.closed=False
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
    async def aclose(self): self.closed=True


def event(delta,finish=None):
    return ('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}]})+'\n\n').encode()


async def test_stream_assembles_and_closes_before_returning():
    stream=SSE([event({'content':'{"summary":'}),event({'content':'"ok"}'},'stop'),b'data: [DONE]\n\n'])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=stream))) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client)
        provider.stream_responses=True
        result=await provider.generate(request())
    assert json.loads(result.content)=={'summary':'ok'}
    assert stream.closed


async def test_truncated_stream_is_never_accepted():
    stream=SSE([event({'content':'{"summary":"looks complete"}'})])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=stream))) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client)
        provider.stream_responses=True
        with pytest.raises(InferenceError,match='partial output discarded'):
            await provider.generate(request())
    assert stream.closed


async def test_stream_cancellation_closes_response():
    entered=asyncio.Event()
    class Slow(SSE):
        async def __aiter__(self):
            entered.set()
            await asyncio.sleep(30)
            yield b''
    stream=Slow([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=stream))) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client);provider.stream_responses=True
        task=asyncio.create_task(provider.generate(request()))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    assert stream.closed


async def test_recovery_requires_inspection_for_identical_failed_fix(tmp_path):
    workspace=Workspace(tmp_path);store=InMemoryStateStore()
    (tmp_path/'app.py').write_text('broken')
    task=make_task('STEP_1',target_files=['app.py'],test_files=['missing.py'])
    decision=OrchestratorResult(decision_summary='fix',decision=OrchestratorDecision.RETRY_WITH_FIX,
        next_payload=NextPayload(target_agent=TargetAgent.WORKER,instruction='Try the same fix'))
    evidence=await evidence_for(store,'demo',task,workspace,'assertion failed')
    assert evidence['files']['missing.py']=='unavailable'
    await record_decision(store,'demo',task,decision,evidence)
    evidence=await evidence_for(store,'demo',task,workspace,'assertion failed')
    changed=await record_decision(store,'demo',task,decision,evidence)
    assert changed.next_payload.instruction.startswith('Fresh inspection required')
    (tmp_path/'app.py').write_text('new evidence')
    evidence=await evidence_for(store,'demo',task,workspace,'assertion failed')
    changed=await record_decision(store,'demo',task,decision,evidence)
    assert changed.next_payload.instruction=='Try the same fix'


async def test_file_hash_changes_and_requirements_survive_packing(tmp_path):
    workspace=Workspace(tmp_path);files=WorkspaceFilesystem(workspace)
    await files.write_file('app.py','before')
    packer=ContextPacker(files)
    task=make_task('STEP_1',target_files=['app.py'],acceptance_criteria=['Never drop this'],test_commands=['pytest'])
    a=await packer.pack(make_project(workspace_root=tmp_path),task)
    await files.write_file('app.py','after')
    b=await packer.pack(make_project(workspace_root=tmp_path),task)
    assert a.files[0].source_sha256 != b.files[0].source_sha256
    assert b.files[0].source_sha256==sha256(b'after').hexdigest()
    assert b.acceptance_criteria==task.acceptance_criteria
    assert b.test_commands==task.test_commands


def test_compaction_preserves_failures_recent_turns_and_source_metadata():
    def pair(ok,content):
        return [ChatMessage(role='assistant',content='{"summary":"read","tool_calls":[]}'),
                ChatMessage(role='user',content='Results:\n'+json.dumps([{'name':'read_file','ok':ok,'output':content,'source_path':'app.py','source_sha256':'hash'}],indent=2))]
    messages=pair(True,'old body')+pair(False,'failure evidence')+pair(True,'latest')
    result=compact_history(messages,retain_pairs=1)
    assert 'old body' not in result[1].content
    assert 'hash' in result[1].content
    assert result[3]==messages[3]
    assert result[-2:]==messages[-2:]
    assert 'old body' in messages[1].content


def test_prompt_examples_do_not_claim_completion_with_tools():
    examples=[json.loads(line) for line in worker.USER_PROMPT.format(context='').splitlines() if line.startswith('{')]
    assert all(not e['done'] or not e['tool_calls'] for e in examples)
    assert 'Use research_search only' not in worker.SYSTEM_PROMPT
    assert 'Node validation prerequisites:' in worker.SYSTEM_PROMPT
    assert 'minimum task count' in planner.SYSTEM_PROMPT


async def test_scripted_evaluation_uses_isolated_acceptance_checks():
    from mobyai.evaluation import evaluate
    report=await evaluate()
    assert report['mode']=='scripted_harness'
    assert report['passed']==report['total']==3
    assert all(case['acceptance_tests_preserved'] for case in report['trials'])
    assert all(case['validation_exit_codes']==[0] for case in report['trials'])
    assert set(report['prompt_hashes'])=={'planner','worker','orchestrator'}


async def test_opt_in_refresh_updates_next_worker_context(tmp_path):
    from tests.unit.test_worker import _ready_store,_write_app_payload
    from mobyai.agents.worker import WorkerAgent
    from mobyai.engine.working import execute_task
    store,workspace=await _ready_store(tmp_path)
    write=_write_app_payload('fresh_unique_contents');write['done']=False
    provider=FakeInferenceProvider([json.dumps(write),'{"summary":"verified","done":true}'])
    settings=RoleModelConfig(model='fake',endpoint='http://local',refresh_worker_context=True,compact_worker_history=True)
    agent=WorkerAgent(StructuredGenerator(provider,settings=settings))
    result=await execute_task(await store.projects.get('proj-1'),'STEP_001',store,agent,workspace)
    assert result.passed
    assert 'fresh_unique_contents' not in provider.requests[0].messages[1].content
    assert 'fresh_unique_contents' in provider.requests[1].messages[1].content
    assert 'sha256=' in provider.requests[1].messages[1].content


async def test_stream_idle_error_does_not_claim_overall_deadline_expired():
    from mobyai.engine.timeouts import TimeoutPolicy
    def handler(request):raise httpx.ReadTimeout('idle')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider=OpenAICompatibleProvider('http://local','model',client=client);provider.stream_responses=True
        policy=await TimeoutPolicy(InMemoryStateStore(),'demo').load()
        with policy.activate(),pytest.raises(InferenceError,match='streaming connection idle'):
            await provider.generate(request())
        assert policy.history==[]

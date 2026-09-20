import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from mobyai.final_pass import service
from mobyai.final_pass.runtime import status, identity
from mobyai.models.config import FinalFailConfig
from mobyai.models.planner import PlannerOutput
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.review import minutes
from tests.helpers import make_project
from tests.unit.test_cli import _runtime, PLAN

async def setup(tmp_path,monkeypatch,review=False):
    path,runtime=_runtime(tmp_path)
    runtime.config.final_fail=FinalFailConfig(enabled=True,directory=tmp_path/'colibri')
    project=make_project(workspace_root=tmp_path/'code').model_copy(update={
        'state':ProjectState.COMPLETED if review else ProjectState.READY,
        'last_error':None if review else 'Schema repair failed: missing summary'})
    project.workspace_root.mkdir()
    (project.workspace_root/'keep.py').write_text('developer work')
    await runtime.store.projects.save(project)
    plan=PlannerOutput.model_validate(PLAN)
    tasks=[s.to_task() for s in plan.tasks]
    if review: tasks=[t.model_copy(update={'status':TaskStatus.PASSED}) for t in tasks]
    await runtime.store.tasks.save_all(project.project_id,tasks)
    @asynccontextmanager
    async def fake_inference(*args): yield SimpleNamespace()
    monkeypatch.setattr(service,'inference',fake_inference)
    create=AsyncMock(return_value=plan)
    monkeypatch.setattr(service.PlannerAgent,'create_plan',create)
    return runtime,project,create

async def test_disabled_is_noop_and_model_default_is_glm(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch)
    runtime.config.final_fail.enabled=False
    assert await service.final_pass(runtime,project) is None
    create.assert_not_called()
    assert FinalFailConfig().model=='zai-org/GLM-5.2'
    assert status(runtime.config.final_fail)['state']=='not_built'

async def test_recovery_handoff_preserves_files_and_runs_once(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch)
    result=await service.final_pass(runtime,project)
    assert result.state==ProjectState.READY and result.project_id==project.project_id
    assert (project.workspace_root/'keep.py').read_text()=='developer work'
    assert (minutes.run_dir(project)/'final_fail.json').is_file()
    assert await service.final_pass(runtime,project) is None
    assert create.await_count==1

@pytest.mark.parametrize('error',['context window exceeded','permission denied','Timeout approval required','out of memory','ConnectError'])
async def test_environment_and_approval_stops_not_bypassed(tmp_path,monkeypatch,error):
    runtime,project,create=await setup(tmp_path,monkeypatch)
    project=project.model_copy(update={'last_error':error})
    assert await service.final_pass(runtime,project) is None
    create.assert_not_called()

async def test_review_opt_in_and_report_distinguishes_outcomes(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch,review=True)
    assert await service.final_pass(runtime,project,review=True) is None
    await service.event(runtime,project,'final_review_enabled',{'enabled':True})
    result=await service.final_pass(runtime,project,review=True)
    assert result.state==ProjectState.READY
    assert await service.final_pass(runtime,result,review=True) is None
    await service.finish_reports(runtime,project)
    report=minutes.read(minutes.run_dir(project)/'final_review.json')
    assert report['status']=='COMPLETED'
    assert all(v['status']=='passed' for v in report['task_outcomes'].values())

async def test_completed_tasks_cannot_be_rewritten(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch,review=True)
    await service.event(runtime,project,'final_review_enabled',{'enabled':True})
    plan=create.return_value.model_copy(deep=True)
    plan.tasks[0].description='Overwrite developer code'
    create.return_value=plan
    with pytest.raises(ValueError,match='completed work'):
        await service.final_pass(runtime,project,review=True)
    assert (await runtime.store.tasks.list_for_project(project.project_id))[0].status==TaskStatus.PASSED

async def test_external_edits_during_planning_reject_handoff(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch)
    async def modified(*args,**kwargs):
        (project.workspace_root/'keep.py').write_text('new developer repair')
        return PlannerOutput.model_validate(PLAN)
    create.side_effect=modified
    assert await service.final_pass(runtime,project) is None
    assert (await runtime.store.projects.get(project.project_id)).last_error==project.last_error
    assert (project.workspace_root/'keep.py').read_text()=='new developer repair'

async def test_cancellation_propagates_without_plan_application(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch)
    create.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await service.final_pass(runtime,project)
    assert (await runtime.store.projects.get(project.project_id)).last_error==project.last_error

async def test_requested_review_failure_cannot_silently_complete_on_resume(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch,review=True)
    await service.event(runtime,project,'final_review_enabled',{'enabled':True})
    create.side_effect=ValueError('not built')
    with pytest.raises(ValueError): await service.final_pass(runtime,project,review=True)
    with pytest.raises(ValueError,match='did not produce'): await service.final_pass(runtime,project,review=True)
    assert create.await_count==1

def test_model_settings_invalidate_build_identity():
    config=FinalFailConfig()
    assert identity(config)!=identity(config.model_copy(update={'weights_repo':'other/model'}))
    assert identity(config)==identity(config.model_copy(update={'enabled':True}))

async def test_prebuild_refuses_insufficient_disk_without_downloading(tmp_path,monkeypatch):
    from mobyai.final_pass import runtime as adapter
    from huggingface_hub import HfApi
    _,runtime=_runtime(tmp_path)
    config=FinalFailConfig(directory=tmp_path/'runtime')
    calls=[]
    async def command(argv,*args,**kwargs): calls.append([str(a) for a in argv])
    monkeypatch.setattr(adapter,'command',command)
    monkeypatch.setattr(adapter.shutil,'which',lambda _: '/bin/tool')
    monkeypatch.setattr(adapter.shutil,'disk_usage',lambda _:SimpleNamespace(free=1))
    monkeypatch.setattr(HfApi,'model_info',lambda *a,**k:SimpleNamespace(sha='fixed',siblings=[SimpleNamespace(size=400*1024**3)]))
    await adapter.build(config,runtime.config)
    assert status(config)['state']=='failed'
    assert 'larger' in status(config)['message']
    assert not any('download' in argv for argv in calls)

async def test_cli_recovery_returns_to_regular_orchestration(tmp_path,monkeypatch):
    import io
    from mobyai.cli import _execute_project_inner
    from mobyai.models.orchestrator import OrchestratorDecision
    runtime,project,create=await setup(tmp_path,monkeypatch)
    calls=[]
    async def run(current,*args,**kwargs):
        calls.append(current)
        if len(calls)==1:
            return SimpleNamespace(project=project,decision=OrchestratorDecision.BLOCKED)
        done=current.model_copy(update={'state':ProjectState.COMPLETED,'last_error':None})
        await runtime.store.projects.save(done)
        return SimpleNamespace(project=done,decision=OrchestratorDecision.MISSION_COMPLETE)
    monkeypatch.setattr('mobyai.cli.run_project',run)
    monkeypatch.setattr('mobyai.review.service.requested_report',AsyncMock())
    assert await _execute_project_inner(runtime,project,io.StringIO(),max_steps=None)==0
    assert len(calls)==2 and calls[1].state==ProjectState.READY
    assert create.await_count==1

async def test_operator_step_limit_never_triggers_final_pass(tmp_path,monkeypatch):
    import io
    from mobyai.cli import _execute_project_inner
    from mobyai.models.orchestrator import OrchestratorDecision
    runtime,project,create=await setup(tmp_path,monkeypatch)
    paused=project.model_copy(update={'state':ProjectState.PAUSED})
    monkeypatch.setattr('mobyai.cli.run_project',AsyncMock(return_value=SimpleNamespace(project=paused,decision=OrchestratorDecision.BLOCKED)))
    monkeypatch.setattr('mobyai.review.service.requested_report',AsyncMock())
    await _execute_project_inner(runtime,project,io.StringIO(),max_steps=1)
    create.assert_not_called()

async def test_review_cancel_leaves_nonterminal_state_for_clean_pause(tmp_path,monkeypatch):
    runtime,project,create=await setup(tmp_path,monkeypatch,review=True)
    await service.event(runtime,project,'final_review_enabled',{'enabled':True})
    create.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await service.final_pass(runtime,project,review=True)
    from mobyai.engine.stopping import pause_interrupted
    paused=await pause_interrupted(runtime.store,project.project_id)
    assert paused.state==ProjectState.PAUSED

async def test_final_generator_checks_context_before_provider_call():
    from mobyai.models.inference import ChatMessage,ModelRequest
    from mobyai.inference.base import InferenceError
    provider=SimpleNamespace(generate=AsyncMock())
    generator=service.FinalGenerator(provider,FinalFailConfig(context_tokens=4096),'Review')
    request=ModelRequest(messages=[ChatMessage(role='user',content='x '*10000)])
    with pytest.raises(InferenceError,match='context capacity'): await generator.generate(request,PlannerOutput)
    provider.generate.assert_not_called()

async def test_adapter_launches_authenticated_local_server_and_cleans_up(tmp_path):
    import os
    from mobyai.final_pass.runtime import paths,publish,inference
    from mobyai.models.inference import ChatMessage,ModelRequest
    config=FinalFailConfig(directory=tmp_path/'runtime',timeout_seconds=60)
    root,source,weights=paths(config)
    (source/'c').mkdir(parents=True)
    (source/'c'/'coli').write_text('''import os,sys,json
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
port=int(sys.argv[sys.argv.index('--port')+1])
model=sys.argv[sys.argv.index('--model-id')+1]
Path(__file__).with_name('pid').write_text(str(os.getpid()))
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def reply(self,body):
  data=json.dumps(body).encode(); self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_GET(self):
  if self.headers.get('Authorization') != 'Bearer '+os.environ['COLI_API_KEY']:
   self.send_error(401); return
  self.reply({'data':[{'id':model}]})
 def do_POST(self):
  if self.headers.get('Authorization') != 'Bearer '+os.environ['COLI_API_KEY']:
   self.send_error(401); return
  json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  self.reply({'model':model,'choices':[{'message':{'content':'READY'},'finish_reason':'stop'}]})
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
''')
    publish(config,'ready','Test fixture')
    async with inference(config) as provider:
        response=await provider.generate(ModelRequest(messages=[ChatMessage(role='user',content='Ready?')],json_mode=False))
        assert response.content=='READY'
        pid=int((source/'c'/'pid').read_text())
    with pytest.raises(ProcessLookupError): os.kill(pid,0)

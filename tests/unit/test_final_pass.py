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
    _,runtime=_runtime(tmp_path)
    model=local_model(tmp_path)
    config=FinalFailConfig(directory=tmp_path/'runtime', model_directory=model)
    calls=[]
    async def command(argv,*args,**kwargs): calls.append([str(a) for a in argv])
    monkeypatch.setattr(adapter,'command',command)
    monkeypatch.setattr(adapter.shutil,'which',lambda _: '/bin/tool')
    monkeypatch.setattr(adapter.shutil,'disk_usage',lambda _:SimpleNamespace(free=1))
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


def test_colibri_accepts_symlinked_storage_base(tmp_path):
    from mobyai.final_pass import runtime as adapter
    storage = tmp_path / 'data-drive'
    storage.mkdir()
    link = tmp_path / 'MobyAI'
    link.symlink_to(storage, target_is_directory=True)
    config = FinalFailConfig(directory=link / 'colibri')
    original_identity = identity(config)
    adapter.publish(config, 'ready', 'Built')
    root, _, _ = adapter.paths(config)
    assert root == storage / 'colibri' / original_identity[:20]
    assert status(config)['state'] == 'ready'
    assert identity(config) == original_identity


def test_colibri_rejects_redirected_build_directory_and_status(tmp_path):
    from mobyai.final_pass import runtime as adapter
    config = FinalFailConfig(directory=tmp_path / 'runtime')
    root, _, _ = adapter.paths(config)
    root.parent.mkdir()
    target = tmp_path / 'outside'
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='symlinked Colibri'):
        adapter.publish(config, 'ready', 'Built')
    root.unlink()
    root.mkdir()
    original = target / 'keep.json'
    original.write_text('untouched')
    (root / 'status.json').symlink_to(original)
    with pytest.raises(ValueError, match='symlinked minutes'):
        adapter.publish(config, 'ready', 'Built')
    assert original.read_text() == 'untouched'


async def test_build_status_failure_is_visible_without_secondary_exception(tmp_path, monkeypatch, caplog):
    from mobyai.final_pass import runtime as adapter
    _, runtime = _runtime(tmp_path)
    config = FinalFailConfig(directory=tmp_path / 'runtime')
    real_write = adapter.write
    def denied(*args):
        raise PermissionError('Read-only storage')
    monkeypatch.setattr(adapter, 'write', denied)
    monkeypatch.setattr(adapter.shutil, 'which', lambda name: '/bin/tool')
    await adapter.build(config, runtime.config)
    assert status(config)['state'] == 'failed'
    assert 'Read-only storage' in status(config)['message']
    assert 'Could not persist' in caplog.text
    monkeypatch.setattr(adapter, 'write', real_write)
    adapter.publish(config, 'ready', 'Recovered')
    assert status(config)['message'] == 'Recovered'
    await runtime.aclose()


async def test_build_cancellation_survives_status_write_failure(tmp_path, monkeypatch):
    from mobyai.final_pass import runtime as adapter
    _, runtime = _runtime(tmp_path)
    config = FinalFailConfig(directory=tmp_path / 'runtime', model_directory=local_model(tmp_path))
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    real_write = adapter.write
    def write(path, data):
        if data.get('state') == 'cancelled':
            raise PermissionError('Read-only storage')
        real_write(path, data)
    monkeypatch.setattr(adapter, 'command', cancel)
    monkeypatch.setattr(adapter, 'write', write)
    monkeypatch.setattr(adapter.shutil, 'which', lambda name: '/bin/tool')
    with pytest.raises(asyncio.CancelledError):
        await adapter.build(config, runtime.config)
    assert status(config)['state'] == 'cancelled'
    adapter._status_fallback.pop(identity(config))
    await runtime.aclose()


def test_progress_log_tail_is_bounded_and_preserves_unicode(tmp_path):
    from mobyai.final_pass.runtime import log_tail
    log = tmp_path / 'build.log'
    log.write_text('old output\n' * 10000 + '\n'.join(f'下載 {i}' for i in range(250)), encoding='utf-8')
    result = log_tail(log)
    assert result['truncated']
    assert len(result['text'].splitlines()) == 200
    assert result['text'].endswith('下載 249')
    assert result['bytes'] == log.stat().st_size
    assert result['updated_at']
    log.write_text('10%\r20%\r100%\n', encoding='utf-8')
    assert log_tail(log)['text'] == '10%\n20%\n100%'


def test_progress_log_refuses_symlinks_and_handles_missing_file(tmp_path):
    from mobyai.final_pass.runtime import log_tail
    log = tmp_path / 'build.log'
    assert log_tail(log)['text'] == ''
    target = tmp_path / 'private'
    target.write_text('private data')
    log.symlink_to(target)
    result = log_tail(log)
    assert 'error' in result
    assert 'private data' not in str(result)


async def test_progress_exposes_subprocess_output_before_completion(tmp_path):
    import sys
    from mobyai.final_pass import runtime as adapter
    config = FinalFailConfig(directory=tmp_path / 'colibri')
    adapter.publish(config, 'ready', 'Test stage')
    root, _, _ = adapter.paths(config)
    task = asyncio.create_task(adapter.command(
        [sys.executable, '-u', '-c', 'import time; print("Compiling worker", flush=True); time.sleep(30)'],
        root / 'build.log'))
    try:
        for _ in range(100):
            result = status(config)
            if 'Compiling worker' in result['logs']['build.log']['text']:
                break
            await asyncio.sleep(.05)
        else:
            pytest.fail('Subprocess output was not visible while running')
        assert not task.done()
        assert 'Test stage' in result['logs']['build.log']['text']
        assert result['updated_at']
        (root / 'inference.log').write_text('Loading model layers…', encoding='utf-8')
        assert 'Loading model layers…' in status(config)['logs']['inference.log']['text']
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def local_model(tmp_path):
    model=tmp_path/'local-model'
    model.mkdir()
    (model/'config.json').write_text('{"model_type":"test"}')
    (model/'model.safetensors').write_bytes(b'local model fixture')
    return model


@pytest.mark.parametrize('placement', ['auto', 'in_place', 'copy'])
async def test_prepare_local_model_placement(tmp_path, monkeypatch, placement):
    from mobyai.final_pass import runtime as adapter
    model=local_model(tmp_path)
    config=FinalFailConfig(directory=tmp_path/'nvme',model_directory=model,model_placement=placement)
    root,_,_=adapter.paths(config)
    root.mkdir(parents=True)
    monkeypatch.setattr(adapter.shutil,'disk_usage',lambda _:SimpleNamespace(free=10**15))
    weights,manifest=await adapter.prepare_weights(config,root,root/'build.log')
    assert (weights/'model.safetensors').read_bytes()==b'local model fixture'
    assert manifest['placement']==('copy' if placement=='copy' else 'in_place')
    assert (weights==model)==(placement!='copy')
    if placement=='copy':
        async def unexpected_copy(*args,**kwargs):
            pytest.fail('Completed files should be reused')
        monkeypatch.setattr(adapter,'command',unexpected_copy)
        assert (await adapter.prepare_weights(config,root,root/'build.log'))[0]==weights
    assert (model/'model.safetensors').read_bytes()==b'local model fixture'


async def test_missing_local_model_fails_without_network(tmp_path, monkeypatch):
    from mobyai.final_pass import runtime as adapter
    _,runtime=_runtime(tmp_path)
    config=FinalFailConfig(directory=tmp_path/'nvme')
    command=AsyncMock()
    monkeypatch.setattr(adapter,'command',command)
    monkeypatch.setattr(adapter.shutil,'which',lambda _: '/bin/tool')
    await adapter.build(config,runtime.config)
    assert status(config)['state']=='failed'
    assert 'Local model directory' in status(config)['message']
    command.assert_not_called()
    await runtime.aclose()


async def test_local_build_uses_prepared_model_without_download(tmp_path, monkeypatch):
    from mobyai.final_pass import runtime as adapter
    _,runtime=_runtime(tmp_path)
    model=local_model(tmp_path)
    config=FinalFailConfig(directory=tmp_path/'nvme',model_directory=model)
    calls=[]
    async def command(argv,*args,**kwargs): calls.append(list(map(str,argv)))
    @asynccontextmanager
    async def fake_inference(config,**kwargs):
        yield SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content='READY')))
    monkeypatch.setattr(adapter,'command',command)
    monkeypatch.setattr(adapter,'inference',fake_inference)
    monkeypatch.setattr(adapter.shutil,'which',lambda _: '/bin/tool')
    monkeypatch.setattr(adapter.shutil,'disk_usage',lambda _:SimpleNamespace(free=10**15))
    await adapter.build(config,runtime.config)
    assert status(config)['state']=='ready'
    assert calls[-1][-2:]==['--model',str(model)]
    assert not any('download' in arg for call in calls for arg in call)
    root,_,_=adapter.paths(config)
    manifest=minutes.read(root/'build-manifest.json')
    assert manifest['smoke_test_passed']
    assert manifest['source_directory']==str(model)
    await runtime.aclose()


async def test_auto_copies_other_storage_and_no_longer_needs_source(tmp_path, monkeypatch):
    from mobyai.final_pass import runtime as adapter
    model=local_model(tmp_path)
    config=FinalFailConfig(directory=tmp_path/'nvme',model_directory=model)
    root,_,_=adapter.paths(config)
    root.mkdir(parents=True)
    monkeypatch.setattr(adapter,'same_storage',lambda *args:False)
    monkeypatch.setattr(adapter.shutil,'disk_usage',lambda _:SimpleNamespace(free=10**15))
    weights,manifest=await adapter.prepare_weights(config,root,root/'build.log')
    assert manifest['placement']=='copy'
    minutes.write(root/'build-manifest.json',manifest)
    model.rename(tmp_path/'disconnected-source')
    assert adapter.prepared_weights(root,None)==weights
    (weights/'model.safetensors').write_bytes(b'changed')
    with pytest.raises(ValueError,match='files changed'):
        adapter.prepared_weights(root,None)


async def test_cancelled_copy_is_not_accepted_as_complete(tmp_path, monkeypatch):
    from mobyai.final_pass import runtime as adapter
    from pathlib import Path
    model=local_model(tmp_path)
    config=FinalFailConfig(directory=tmp_path/'nvme',model_directory=model,model_placement='copy')
    root,_,_=adapter.paths(config)
    root.mkdir(parents=True)
    monkeypatch.setattr(adapter.shutil,'disk_usage',lambda _:SimpleNamespace(free=10**15))
    command=adapter.command
    async def cancel(argv,*args,**kwargs):
        Path(argv[-1]).write_bytes(b'incomplete')
        raise asyncio.CancelledError()
    monkeypatch.setattr(adapter,'command',cancel)
    with pytest.raises(asyncio.CancelledError):
        await adapter.prepare_weights(config,root,root/'build.log')
    assert not list((root/'weights').rglob('config.json'))
    monkeypatch.setattr(adapter,'command',command)
    weights,_=await adapter.prepare_weights(config,root,root/'build.log')
    assert (weights/'config.json').read_bytes()==(model/'config.json').read_bytes()


def test_local_model_config_round_trip_and_legacy_compatibility(tmp_path):
    from pydantic import ValidationError
    from mobyai.console.settings import serialize_role_settings, apply_role_settings
    _,runtime=_runtime(tmp_path)
    config=runtime.config
    payload=serialize_role_settings(config)
    payload['final_fail'].update(model_directory=str(tmp_path/'models'),model_placement='copy')
    apply_role_settings(config,payload)
    assert config.final_fail.model_directory==tmp_path/'models'
    assert config.final_fail.model_placement=='copy'
    assert FinalFailConfig(weights_repo='legacy/model').model_directory is None
    with pytest.raises(ValidationError):
        FinalFailConfig(model_directory='relative/model')
    with pytest.raises(ValidationError):
        FinalFailConfig(model_placement='download')

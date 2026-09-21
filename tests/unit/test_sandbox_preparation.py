import io
from pathlib import Path
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from mobyai.sandbox.policy import SandboxConfig, current
from mobyai.sandbox.builder import SandboxPreparationError, RECIPE, REQUIREMENTS, recipe_key, ensure
from mobyai.sandbox.preparation import run_prepared
from mobyai.state.memory import InMemoryStateStore

IMAGE='sha256:'+'a'*64

def test_auto_policy_is_bounded():
    assert SandboxConfig(enabled=True,auto_prepare=True).image==''
    with pytest.raises(ValidationError):SandboxConfig(enabled=True)
    with pytest.raises(ValidationError):SandboxConfig(enabled=True,auto_prepare=True,image='bad')
    with pytest.raises(ValidationError):SandboxConfig(enabled=True,auto_prepare=True,profile='arbitrary-recipe')
    assert 'COPY requirements.txt' in RECIPE and 'COPY .' not in RECIPE
    assert '--only-binary=:all:' in RECIPE
    assert '@sha256:' in RECIPE
    assert len(recipe_key())==64

async def test_cached_image_preparation_does_not_build(monkeypatch,tmp_path):
    from mobyai.sandbox import builder
    import json
    monkeypatch.setattr(builder.os,'geteuid',lambda:1000)
    monkeypatch.setattr(builder.Path,'home',lambda:tmp_path)
    async def control(*args):
        assert args==('info','--format','{{.Host.Security.Rootless}}')
        return 'true'
    async def installed(image):return IMAGE
    monkeypatch.setattr(builder,'control',control)
    monkeypatch.setattr(builder,'installed',installed)
    folder=tmp_path/'.cache/mobyai-sandbox/images'/recipe_key();folder.mkdir(parents=True)
    (folder/'image.json').write_text(json.dumps({'image':IMAGE,'recipe_key':recipe_key()}))
    result=await ensure(SandboxConfig(enabled=True,auto_prepare=True),30,'mobyai-sandbox-'+'b'*32)
    assert result['cached'] and result['image']==IMAGE

async def test_preparation_precedes_work_and_activates_pinned_image(monkeypatch):
    store=InMemoryStateStore()
    config=SandboxConfig(enabled=True,auto_prepare=True)
    runtime=SimpleNamespace(config=SimpleNamespace(sandbox=config),store=store)
    project=SimpleNamespace(project_id='sandbox-test')
    async def prepare(config,seconds):
        assert seconds==600
        return {'image':IMAGE,'cached':True}
    async def operation():
        assert current().image==IMAGE
        assert not current().auto_prepare
        return 'worked'
    monkeypatch.setattr('mobyai.sandbox.preparation.call_prepare',prepare)
    assert await run_prepared(runtime,project,io.StringIO(),operation)=='worked'
    assert config.image==''
    assert current() is None
    assert [e.event for e in await store.events.list_for_project(project.project_id)]==['sandbox_preparing','sandbox_ready']

async def test_failed_preparation_never_starts_work(monkeypatch):
    store=InMemoryStateStore()
    runtime=SimpleNamespace(config=SimpleNamespace(sandbox=SandboxConfig(enabled=True,auto_prepare=True)),store=store)
    async def prepare(*args):raise SandboxPreparationError('download failed')
    async def operation():raise AssertionError('work started without isolation')
    monkeypatch.setattr('mobyai.sandbox.preparation.call_prepare',prepare)
    with pytest.raises(SandboxPreparationError):
        await run_prepared(runtime,SimpleNamespace(project_id='p'),io.StringIO(),operation)
    assert (await store.events.list_for_project('p'))[-1].event=='sandbox_preparation_failed'

async def test_preparation_failure_pauses_cli_with_reason(tmp_path,monkeypatch):
    from tests.unit.test_cli_ssh import create
    from mobyai.cli import _execute_project
    runtime,project=await create(tmp_path)
    runtime.config.runtime.keep_awake=False
    runtime.config.sandbox=SandboxConfig(enabled=True,auto_prepare=True)
    async def prepare(*args):raise SandboxPreparationError('dependency mirror unavailable')
    async def forbidden(*args,**kwargs):raise AssertionError('worker started')
    monkeypatch.setattr('mobyai.sandbox.preparation.call_prepare',prepare)
    monkeypatch.setattr('mobyai.cli._execute_project_inner',forbidden)
    output=io.StringIO()
    assert await _execute_project(runtime,project,output,max_steps=None)==0
    saved=await runtime.store.projects.get(project.project_id)
    assert saved.state.value=='PAUSED'
    assert 'dependency mirror unavailable' in saved.last_error

async def test_pausing_cancels_preparation(monkeypatch):
    import asyncio
    started=asyncio.Event();cancelled=asyncio.Event()
    async def prepare(*args):
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    async def operation():raise AssertionError('work started')
    monkeypatch.setattr('mobyai.sandbox.preparation.call_prepare',prepare)
    runtime=SimpleNamespace(config=SimpleNamespace(sandbox=SandboxConfig(enabled=True,auto_prepare=True)),store=InMemoryStateStore())
    task=asyncio.create_task(run_prepared(runtime,SimpleNamespace(project_id='p'),io.StringIO(),operation))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert cancelled.is_set()

async def test_preparation_timeout_uses_existing_ladder(monkeypatch):
    from mobyai.engine.timeouts import TimeoutPolicy
    from tests.helpers import make_project
    store=InMemoryStateStore();project=make_project();await store.projects.save(project)
    runtime=SimpleNamespace(config=SimpleNamespace(sandbox=SandboxConfig(enabled=True,auto_prepare=True)),store=store)
    async def prepare(*args):return {'timed_out':True}
    async def operation():raise AssertionError('work started')
    monkeypatch.setattr('mobyai.sandbox.preparation.call_prepare',prepare)
    policy=await TimeoutPolicy(store,project.project_id).load()
    with policy.activate():
        with pytest.raises(SandboxPreparationError,match='750'):
            await run_prepared(runtime,project,io.StringIO(),operation)
    assert policy.history[-1]['next_seconds']==750

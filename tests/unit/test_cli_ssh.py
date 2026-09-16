import asyncio
from datetime import datetime, UTC
from io import StringIO

import pytest

from mobyai.cli import async_main
from mobyai.cli_operations import run_with_pause, PauseRequested, snapshot
from mobyai.engine.lease import project_lease
from mobyai.models.event import EventRecord
from tests.unit.test_cli import _runtime


async def create(tmp_path):
    _, runtime = _runtime(tmp_path)
    source = tmp_path / 'prompt.txt'
    source.write_text('Build a small API.\nKeep these acceptance criteria.\n')
    target = tmp_path / 'target'
    code = await async_main(['init','--name','ssh-demo','--prompt-file',str(source),
                             '--target-directory',str(target)], runtime=runtime, stdout=StringIO())
    assert code == 0
    project = await runtime.store.projects.get('ssh-demo')
    assert project.objective == source.read_text().strip()
    assert project.workspace_root == target
    return runtime, project


async def test_prompt_target_and_safe_deletion(tmp_path):
    runtime, project = await create(tmp_path)
    file = project.workspace_root / 'keep.txt'
    file.write_text('keep')
    err=StringIO()
    assert await async_main(['delete','ssh-demo','--confirm','wrong'],runtime=runtime,stderr=err) == 1
    with project_lease(runtime.config.persistence.database_path,'ssh-demo'):
        assert await async_main(['delete','ssh-demo','--confirm','ssh-demo'],runtime=runtime,stderr=err) == 1
    assert await async_main(['delete','ssh-demo','--confirm','ssh-demo'],runtime=runtime,stdout=StringIO()) == 0
    assert await runtime.store.projects.get('ssh-demo') is None
    assert file.read_text() == 'keep'


@pytest.mark.parametrize('content',[b'',b'\xff',b'x'*1_048_577])
async def test_bad_prompt_creates_no_project(tmp_path,content):
    _, runtime = _runtime(tmp_path)
    source=tmp_path/'bad.txt';source.write_bytes(content)
    assert await async_main(['init','--prompt-file',str(source)],runtime=runtime,stderr=StringIO()) == 1
    assert await runtime.store.projects.list_ids() == []


async def test_pause_cancels_runner_and_stale_request_does_not(tmp_path):
    runtime,project=await create(tmp_path)
    await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC),project_id=project.project_id,component='cli',event='pause_requested'))
    entered=asyncio.Event();cancelled=asyncio.Event()
    async def operation():
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
    runner=asyncio.create_task(run_with_pause(runtime,project.project_id,operation))
    await entered.wait()
    await asyncio.sleep(0.6)
    assert not runner.done()
    await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC),project_id=project.project_id,component='cli',event='pause_requested'))
    with pytest.raises(PauseRequested):
        await asyncio.wait_for(runner,3)
    assert cancelled.is_set()


async def test_monitor_verbosity_and_terminal_escaping(tmp_path):
    runtime, project=await create(tmp_path)
    await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC),project_id=project.project_id,component='planner',event='planning',payload={'detail':'test detail\x1b'}))
    basic,_=await snapshot(runtime,project.project_id,1)
    verbose,_=await snapshot(runtime,project.project_id,2)
    assert 'test detail' not in basic
    assert 'planner.planning' in verbose
    assert '\x1b' not in verbose


async def test_secret_stdin_does_not_echo(tmp_path,monkeypatch):
    saved=[]
    monkeypatch.setattr('mobyai.vault.Vault.save',lambda self,p,v:saved.append((p,v)))
    out=StringIO();err=StringIO()
    assert await async_main(['secrets','set','huggingface','--file','-'],stdin=StringIO('hf_private'),stdout=out,stderr=err) == 0
    assert saved == [('huggingface','hf_private')]
    assert 'hf_private' not in out.getvalue()+err.getvalue()


async def test_secret_remove_confirmation(monkeypatch):
    removed=[]
    monkeypatch.setattr('mobyai.vault.Vault.remove',lambda self,p:removed.append(p))
    assert await async_main(['secrets','remove','github','--confirm','no'],stderr=StringIO()) == 1
    assert not removed


async def test_cli_pause_waits_for_runner_cleanup(tmp_path, monkeypatch):
    runtime,project=await create(tmp_path)
    runtime.config.runtime.keep_awake=False
    entered=asyncio.Event();cleaned=asyncio.Event()
    async def work(*args,**kwargs):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cleaned.set()
    monkeypatch.setattr('mobyai.cli._execute_project_inner',work)
    from mobyai.cli import _execute_project
    out=StringIO()
    runner=asyncio.create_task(_execute_project(runtime,project,out,max_steps=None))
    await entered.wait()
    request=StringIO()
    assert await async_main(['pause',project.project_id],runtime=runtime,stdout=request)==0
    assert 'pause requested' in request.getvalue()
    assert await asyncio.wait_for(runner,3)==0
    assert cleaned.is_set()
    saved=await runtime.store.projects.get(project.project_id)
    assert saved.state.value=='PAUSED'
    assert 'PAUSED' in out.getvalue()

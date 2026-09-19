import asyncio
from unittest.mock import AsyncMock
import pytest
from tests.unit.test_cli import _runtime
from mobyai.console.workloads import Workloads


async def test_import_preserves_prompt_and_launches_by_id(tmp_path, monkeypatch):
    path, runtime = _runtime(tmp_path, sqlite=True)
    await runtime.initialize()
    process = AsyncMock()
    process.returncode = None
    gate = asyncio.Event()
    async def wait():
        await gate.wait()
        process.returncode = 0
        return 0
    process.wait.side_effect = wait
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    workloads = Workloads(runtime, path)
    prompt = '  # Contract\nExact requirements\n' * 500
    result = await workloads.create({'name': 'Imported workload', 'prompt': prompt})
    project = await runtime.store.projects.get(result['project_id'])
    assert project.objective == prompt
    assert project.workspace_root.is_dir()
    assert prompt not in spawn.call_args.args
    assert spawn.call_args.args[-2:] == ('run', result['project_id'])
    with pytest.raises(ValueError, match='already running'):
        await workloads.create({'name':'Second', 'prompt':'test'})
    gate.set()
    await workloads.watcher
    events = await runtime.store.events.list_for_project(result['project_id'])
    assert events[-1].event == 'workload_exited'
    await runtime.aclose()


@pytest.mark.parametrize('payload', [None, {}, {'name':'test','prompt':''},
    {'name':'test','prompt':'x'*1_000_001}, {'name':'test','prompt':'bad\x00text'}])
async def test_invalid_import_does_not_launch(tmp_path, payload, monkeypatch):
    path, runtime = _runtime(tmp_path)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(ValueError):
        await Workloads(runtime, path).create(payload)
    spawn.assert_not_called()


async def test_launch_failure_keeps_imported_project(tmp_path, monkeypatch):
    path, runtime = _runtime(tmp_path, sqlite=True)
    await runtime.initialize()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=OSError('cannot spawn')))
    try:
        with pytest.raises(ValueError, match='saved but launch failed'):
            await Workloads(runtime, path).create({'name': 'Retained', 'prompt': 'Full requirements'})
        ids = await runtime.store.projects.list_ids()
        assert len(ids) == 1
        assert (await runtime.store.projects.get(ids[0])).objective == 'Full requirements'
        events = await runtime.store.events.list_for_project(ids[0])
        assert events[-1].event == 'workload_launch_failed'
    finally:
        await runtime.aclose()


async def test_resume_keeps_project_plan_and_files(tmp_path, monkeypatch):
    from tests.unit.test_reliability import project
    from mobyai.models.project import ProjectState
    from mobyai.models.task import Task, TaskStatus
    path, runtime = _runtime(tmp_path)
    item = project(tmp_path, ProjectState.PAUSED).model_copy(update={'paused_from':ProjectState.READY})
    await runtime.store.projects.save(item)
    await runtime.store.tasks.save(item.project_id, Task(task_id='DONE',name='Done',description='Done',status=TaskStatus.PASSED))
    (tmp_path/'app.txt').write_text('saved work')
    workloads = Workloads(runtime, path)
    workloads._launch = AsyncMock(return_value={'project_id':item.project_id})
    result = await workloads.resume({'project_id':item.project_id})
    assert result['project_id'] == item.project_id
    workloads._launch.assert_awaited_once_with(item, 'resume')
    assert (await runtime.store.tasks.get(item.project_id,'DONE')).status == TaskStatus.PASSED
    assert (tmp_path/'app.txt').read_text() == 'saved work'


async def test_restart_creates_fresh_run_and_preserves_original(tmp_path, monkeypatch):
    from tests.unit.test_reliability import project
    from mobyai.models.project import ProjectState
    path, runtime = _runtime(tmp_path)
    original_dir = tmp_path/'original'; original_dir.mkdir()
    item = project(original_dir, ProjectState.FAILED)
    await runtime.store.projects.save(item)
    (original_dir/'work.txt').write_text('original')
    workloads = Workloads(runtime, path)
    async def launched(new, command):
        return {'project_id':new.project_id}
    workloads._launch = AsyncMock(side_effect=launched)
    with pytest.raises(ValueError):
        await workloads.restart({'project_id':item.project_id})
    destination = tmp_path/'new target'
    result = await workloads.restart({'project_id':item.project_id,'confirmation':item.project_id,'target_directory':str(destination)})
    fresh = await runtime.store.projects.get(result['project_id'])
    assert fresh.project_id != item.project_id
    assert fresh.objective == item.objective
    assert fresh.state == ProjectState.NEW
    assert fresh.workspace_root == destination
    assert not list(destination.iterdir())
    assert (await runtime.store.projects.get(item.project_id)).state == ProjectState.FAILED
    assert (original_dir/'work.txt').read_text() == 'original'


async def test_change_target_copies_files_and_rebases_log_paths(tmp_path):
    from tests.unit.test_reliability import project
    from mobyai.models.project import ProjectState
    from mobyai.models.attempt import TaskAttempt
    from datetime import UTC, datetime
    path, runtime = _runtime(tmp_path)
    source = tmp_path/'source'; source.mkdir()
    (source/'test.log').write_text('test output')
    item = project(source, ProjectState.PAUSED).model_copy(update={'paused_from':ProjectState.READY})
    await runtime.store.projects.save(item)
    await runtime.store.attempts.save(TaskAttempt(project_id=item.project_id,task_id='STEP_1',attempt_number=1,
        started_at=datetime.now(UTC),test_log_path=str(source/'test.log')))
    workloads = Workloads(runtime,path)
    destination = tmp_path/'destination'
    result = await workloads.change_target({'project_id':item.project_id,'target_directory':str(destination)})
    assert result['status'] == 'copying'
    assert not workloads.status(item.project_id,'PAUSED')['can_resume']
    await workloads.move_task
    assert workloads.relocation['state'] == 'complete'
    assert (destination/'test.log').read_text() == 'test output'
    assert (source/'test.log').exists()
    assert (await runtime.store.projects.get(item.project_id)).workspace_root == destination
    assert (await runtime.store.attempts.get(item.project_id,'STEP_1',1)).test_log_path == 'test.log'


async def test_copy_failure_keeps_original_target(tmp_path, monkeypatch):
    from tests.unit.test_reliability import project
    from mobyai.models.project import ProjectState
    path, runtime = _runtime(tmp_path)
    source = tmp_path/'source'; source.mkdir()
    item = project(source,ProjectState.PAUSED)
    await runtime.store.projects.save(item)
    workloads = Workloads(runtime,path)
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr('mobyai.console.workloads.shutil.copytree',fail)
    await workloads.change_target({'project_id':item.project_id,'target_directory':str(tmp_path/'destination')})
    await workloads.move_task
    assert workloads.relocation['state'] == 'failed'
    assert (await runtime.store.projects.get(item.project_id)).workspace_root == source


async def test_target_rejects_nonempty_relative_and_overlapping_paths(tmp_path):
    from tests.unit.test_reliability import project
    from mobyai.models.project import ProjectState
    path, runtime = _runtime(tmp_path)
    source = tmp_path/'source'; source.mkdir()
    item = project(source,ProjectState.PAUSED)
    await runtime.store.projects.save(item)
    workloads = Workloads(runtime,path)
    nonempty = tmp_path/'nonempty'; nonempty.mkdir(); (nonempty/'keep').write_text('keep')
    for target in ['relative/path', str(source/'nested'), str(tmp_path), str(nonempty)]:
        with pytest.raises(ValueError):
            await workloads.change_target({'project_id':item.project_id,'target_directory':target})
    assert (nonempty/'keep').read_text() == 'keep'
    with pytest.raises(ValueError, match='overlaps'):
        await workloads.create({'name':'Other','prompt':'Build','target_directory':str(source)})


async def test_active_and_terminal_state_action_guards(tmp_path):
    from tests.unit.test_reliability import project
    from mobyai.models.project import ProjectState
    from mobyai.engine.lease import project_lease
    path, runtime = _runtime(tmp_path)
    workloads = Workloads(runtime,path)
    for state in [ProjectState.WORKING,ProjectState.COMPLETED,ProjectState.FAILED]:
        item = project(tmp_path,state)
        await runtime.store.projects.save(item)
        with pytest.raises(ValueError):
            await workloads.resume({'project_id':item.project_id})
    item = project(tmp_path,ProjectState.PAUSED)
    await runtime.store.projects.save(item)
    with project_lease(runtime.config.persistence.database_path,item.project_id):
        assert not workloads.status(item.project_id,'PAUSED')['can_restart']
        with pytest.raises(ValueError):
            await workloads.resume({'project_id':item.project_id})


async def test_cli_duplicate_resume_does_not_change_paused_state(tmp_path):
    from io import StringIO
    from mobyai.cli import async_main
    from tests.unit.test_reliability import project
    from mobyai.models.project import ProjectState
    from mobyai.engine.lease import project_lease
    path, runtime = _runtime(tmp_path)
    item = project(tmp_path,ProjectState.PAUSED).model_copy(update={'paused_from':ProjectState.READY})
    await runtime.store.projects.save(item)
    errors = StringIO()
    with project_lease(runtime.config.persistence.database_path,item.project_id):
        code = await async_main(['--config',str(path),'resume',item.project_id],runtime=runtime,stdout=StringIO(),stderr=errors)
    assert code != 0
    assert 'already running' in errors.getvalue()
    assert (await runtime.store.projects.get(item.project_id)).state == ProjectState.PAUSED

async def test_exited_worker_reconciles_orphan_and_enables_delete(tmp_path):
    from tests.helpers import make_project
    from mobyai.models.project import ProjectState
    path,runtime=_runtime(tmp_path)
    project=make_project(workspace_root=tmp_path/'workspace').model_copy(update={'state':ProjectState.WORKING,'last_error':'Schema repair failed'})
    await runtime.store.projects.save(project)
    workloads=Workloads(runtime,path)
    process=AsyncMock()
    process.returncode=1
    process.wait.return_value=1
    await workloads.watch(process,project.project_id)
    saved=await runtime.store.projects.get(project.project_id)
    assert saved.state==ProjectState.PAUSED
    assert saved.last_error=='Schema repair failed'
    assert workloads.status(project.project_id,saved.state)['can_delete']

async def test_exited_worker_does_not_pause_new_owner(tmp_path):
    from tests.helpers import make_project
    from mobyai.models.project import ProjectState
    from mobyai.engine.lease import project_lease
    path,runtime=_runtime(tmp_path)
    project=make_project().model_copy(update={'state':ProjectState.WORKING})
    await runtime.store.projects.save(project)
    workloads=Workloads(runtime,path)
    process=AsyncMock()
    process.wait.return_value=1
    with project_lease(runtime.config.persistence.database_path,project.project_id):
        await workloads.watch(process,project.project_id)
    assert (await runtime.store.projects.get(project.project_id)).state==ProjectState.WORKING

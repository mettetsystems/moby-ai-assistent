import pytest
from types import SimpleNamespace
from tests.helpers import make_project,make_task,FROZEN_NOW
from mobyai.state.memory import InMemoryStateStore
from mobyai.state.sqlite import SqliteStateStore
from mobyai.models.project import ProjectState
from mobyai.models.attempt import TaskAttempt
from mobyai.models.event import EventRecord
from mobyai.console.workloads import Workloads
from tests.unit.test_cli import _runtime
from mobyai.engine.lease import project_lease

@pytest.mark.parametrize('sqlite',[False,True])
async def test_delete_records_preserves_files_and_other_projects(tmp_path,sqlite):
    store=SqliteStateStore(tmp_path/'data.db') if sqlite else InMemoryStateStore()
    root=tmp_path/'workspace';root.mkdir();(root/'keep.txt').write_text('keep')
    item=make_project(workspace_root=root)
    await store.projects.save(item)
    await store.projects.save(item.model_copy(update={'project_id':'other'}))
    await store.tasks.save(item.project_id,make_task('STEP_001'))
    await store.attempts.save(TaskAttempt(project_id=item.project_id,task_id='STEP_001',attempt_number=1,started_at=FROZEN_NOW))
    await store.events.append(EventRecord(timestamp=FROZEN_NOW,project_id=item.project_id,component='test',event='saved'))
    await store.delete_project(item.project_id)
    assert await store.load_snapshot(item.project_id) is None
    assert not await store.tasks.list_for_project(item.project_id)
    assert not await store.attempts.list_for_project(item.project_id)
    assert not await store.events.list_for_project(item.project_id)
    assert await store.projects.get('other')
    assert (root/'keep.txt').read_text()=='keep'
    if sqlite:await store.close()

async def test_delete_confirmation_and_execution_lock(tmp_path):
    path,runtime=_runtime(tmp_path)
    item=make_project(workspace_root=tmp_path,state=ProjectState.PAUSED)
    await runtime.store.projects.save(item)
    workloads=Workloads(runtime,path)
    with pytest.raises(ValueError,match='exact project ID'):
        await workloads.delete({'project_id':item.project_id})
    with project_lease(runtime.config.persistence.database_path,item.project_id):
        with pytest.raises(ValueError,match='running'):
            await workloads.delete({'project_id':item.project_id,'confirmation':item.project_id})
    result=await workloads.delete({'project_id':item.project_id,'confirmation':item.project_id})
    assert result['deleted_project_id']==item.project_id

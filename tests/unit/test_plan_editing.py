import io
import pytest
from mobyai.review import editing, minutes
from mobyai.review.service import approve, approved
from mobyai.cli import _execute_project_inner
from tests.unit.test_existing_review import existing

async def prepared(tmp_path, monkeypatch):
    runtime,project,workspace,_=await existing(tmp_path,monkeypatch)
    await _execute_project_inner(runtime,project,io.StringIO(),max_steps=None)
    project=await runtime.store.projects.get(project.project_id)
    return runtime,project,workspace

async def test_edit_initial_plan_same_project_requires_latest_approval(tmp_path,monkeypatch):
    runtime,project,workspace=await prepared(tmp_path,monkeypatch)
    before=(workspace/'main.py').read_bytes()
    draft=await editing.snapshot(runtime.store,project)
    original_revision=minutes.read(minutes.run_dir(project)/'proposal.json')['revision']
    draft['plan']['tasks'][0]['description']='Preserve the entrypoint and improve its tests'
    saved=await editing.save(runtime,project,{**draft,'reason':'Clarify scope'})
    assert not await approved(runtime.store,project)
    with pytest.raises(ValueError,match='changed'):
        await approve(runtime,project,original_revision)
    await approve(runtime,project,saved['revision'])
    assert await approved(runtime.store,project)
    assert (workspace/'main.py').read_bytes()==before
    assert (await runtime.store.projects.get(project.project_id)).objective==project.objective
    assert list((minutes.run_dir(project)/'plan-edits').glob('*.json'))
    with pytest.raises(ValueError,match='first approval'):
        await editing.snapshot(runtime.store,project)

async def test_stale_and_invalid_edits_do_not_change_plan(tmp_path,monkeypatch):
    runtime,project,_=await prepared(tmp_path,monkeypatch)
    draft=await editing.snapshot(runtime.store,project)
    original=minutes.read(minutes.run_dir(project)/'proposal.json')
    with pytest.raises(ValueError,match='changed'):
        await editing.save(runtime,project,{**draft,'version':'stale','reason':'Change'})
    draft['plan']['tasks'][0]['dependencies']=['missing-task']
    with pytest.raises(ValueError):
        await editing.save(runtime,project,{**draft,'reason':'Change'})
    assert minutes.read(minutes.run_dir(project)/'proposal.json')==original

async def test_codebase_change_blocks_editor(tmp_path,monkeypatch):
    runtime,project,workspace=await prepared(tmp_path,monkeypatch)
    (workspace/'main.py').write_text('changed')
    with pytest.raises(ValueError,match='Codebase changed'):
        await editing.snapshot(runtime.store,project)

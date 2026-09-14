import io
import json
import sqlite3
from unittest.mock import AsyncMock
import pytest
from tests.unit.test_cli import _runtime, PLAN
from tests.helpers import make_project
from mobyai.review import graph, minutes
from mobyai.review.service import event, prepare, approve, ensure_approved, logic_report
from mobyai.review.agents import ReviewReport
from mobyai.console.workloads import Workloads
from mobyai.cli import _execute_project_inner
from mobyai.models.project import ProjectState
from mobyai.engine.working import execute_task
from mobyai.execution.workspace import Workspace

async def existing(tmp_path, monkeypatch, native=False):
    path,runtime=_runtime(tmp_path,planner=[json.dumps({**PLAN,'review_influence':['Preserve existing entry point; add tests identified by functionality review.']})])
    workspace=tmp_path/'code'; workspace.mkdir()
    (workspace/'main.py').write_text('def greet():\n    return "hello"\n')
    project=make_project(workspace_root=workspace)
    if native: minutes.initialize(project)
    workloads=Workloads(runtime,path)
    workloads._launch=AsyncMock(side_effect=lambda p,c:{'project_id':p.project_id})
    result=await workloads.create({'workflow':'existing','name':'Improve','prompt':'Add tests','target_directory':str(workspace)})
    project=await runtime.store.projects.get(result['project_id'])
    review=AsyncMock(return_value=ReviewReport(summary='Greeting function exists',findings=[],limitations=['No tests executed']))
    monkeypatch.setattr('mobyai.review.agents.ReviewerAgent.review',review)
    return runtime,project,workspace,review

async def test_external_review_stops_before_writes_and_requires_exact_approval(tmp_path,monkeypatch):
    runtime,project,workspace,review=await existing(tmp_path,monkeypatch)
    original=(workspace/'main.py').read_bytes()
    output=io.StringIO()
    assert await _execute_project_inner(runtime,project,output,max_steps=None)==0
    saved=await runtime.store.projects.get(project.project_id)
    assert saved.state==ProjectState.PAUSED
    assert 'human review' in output.getvalue()
    assert (workspace/'main.py').read_bytes()==original
    assert not (workspace/'src').exists()
    assert (minutes.run_dir(project)/'code-graph.sqlite3').exists()
    proposal=minutes.read(minutes.run_dir(project)/'proposal.json')
    assert proposal['plan']['review_influence']
    with pytest.raises(ValueError,match='approval'):
        await ensure_approved(runtime.store,project)
    with pytest.raises(ValueError,match='approval'):
        await execute_task(saved,'STEP_001',runtime.store,None,Workspace(workspace))
    with pytest.raises(ValueError,match='changed'):
        await approve(runtime,project,'wrong')
    await approve(runtime,project,proposal['revision'])
    await ensure_approved(runtime.store,project)

async def test_changed_source_invalidates_approval(tmp_path,monkeypatch):
    runtime,project,workspace,_=await existing(tmp_path,monkeypatch)
    await _execute_project_inner(runtime,project,io.StringIO(),max_steps=None)
    proposal=minutes.read(minutes.run_dir(project)/'proposal.json')
    (workspace/'main.py').write_text('changed')
    with pytest.raises(ValueError,match='Codebase changed'):
        await approve(runtime,project,proposal['revision'])

async def test_native_uses_history_not_archaeology(tmp_path,monkeypatch):
    runtime,project,workspace,review=await existing(tmp_path,monkeypatch,native=True)
    build=AsyncMock(side_effect=AssertionError('No archaeology for native codebase'))
    monkeypatch.setattr('mobyai.review.service.functionality_report',build)
    result=await prepare(runtime,project)
    assert result['mode']=='native'
    assert result['report']['limitations']==['No archived planner history exists. Historical reasoning cannot be reconstructed.']
    build.assert_not_called()

async def test_plain_greenfield_create_still_rejects_existing_files(tmp_path):
    path,runtime=_runtime(tmp_path)
    root=tmp_path/'code';root.mkdir();(root/'keep.py').write_text('keep')
    with pytest.raises(ValueError,match='empty'):
        await Workloads(runtime,path).create({'name':'New','prompt':'Build','target_directory':str(root)})
    assert (root/'keep.py').read_text()=='keep'

async def test_report_failure_does_not_start_implementation(tmp_path,monkeypatch):
    runtime,project,workspace,review=await existing(tmp_path,monkeypatch)
    review.side_effect=ValueError('model unavailable')
    assert await _execute_project_inner(runtime,project,io.StringIO(),max_steps=None)==1
    saved=await runtime.store.projects.get(project.project_id)
    assert saved.state==ProjectState.PAUSED
    assert 'model unavailable' in saved.last_error
    assert not await runtime.store.tasks.list_for_project(project.project_id)


def test_python_graph_neighbors_and_exclusions(tmp_path):
    root=tmp_path/'code';root.mkdir()
    (root/'app.py').write_text('def helper():\n    return 1\ndef entry():\n    return helper()\n')
    (root/'node_modules').mkdir();(root/'node_modules'/'skip.py').write_text('bad')
    (root/'secrets.json').write_text('{"password":"secret"}')
    (root/'escape.py').symlink_to('/etc/passwd')
    db=tmp_path/'graph.sqlite3'
    meta=graph.build(root,db)
    assert meta['files']==1
    hits=graph.retrieve(db,'entry',limit=1)
    assert any(h['name']=='helper' and h['retrieval']=='graph_neighbor' for h in hits)
    assert all(h['path']=='app.py' for h in hits)


def test_tree_sitter_javascript_symbols(tmp_path):
    pytest.importorskip('tree_sitter_javascript')
    (tmp_path/'app.js').write_text('function greet() { return "hello"; }\nfunction main() { return greet(); }')
    db=tmp_path/'graph.sqlite3'
    meta=graph.build(tmp_path,db)
    assert not meta['warnings']
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT count(*) FROM edges WHERE kind="call_candidate"').fetchone()[0]>=1


def test_minutes_symlink_is_rejected(tmp_path):
    root=tmp_path/'code';root.mkdir();other=tmp_path/'other';other.mkdir()
    (root/'mobyMinutes').symlink_to(other,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):
        minutes.initialize(make_project(workspace_root=root))
    assert list(other.iterdir())==[]

async def test_planning_archive_contains_prompt_plan_raw_response_and_reasoning(tmp_path):
    from mobyai.engine.planning import plan_project
    from mobyai.models.inference import ModelResponse
    from tests.unit.test_planner import _agent, VALID_PLAN
    from mobyai.state.memory import InMemoryStateStore
    store=InMemoryStateStore();workspace=Workspace(tmp_path/'code')
    project=make_project(workspace_root=workspace.root)
    await store.projects.save(project)
    provider,agent=_agent([])
    provider.generate=AsyncMock(return_value=ModelResponse(content=json.dumps(VALID_PLAN),model='Qwen3',reasoning_content='Model-provided rationale'))
    await plan_project(project,store,agent,workspace=workspace)
    record=minutes.plan_records(project)[0]
    assert record['prompt']==project.objective
    from mobyai.models.planner import PlannerOutput
    assert record['plan']==PlannerOutput.model_validate(VALID_PLAN).model_dump(mode='json')
    assert record['calls'][0]['response']['reasoning_content']=='Model-provided rationale'
    assert record['calls'][0]['request']['messages']
    assert record['status']=='ok'

async def test_real_planner_mlflow_spans_export_locally_without_remote_payloads(tmp_path,monkeypatch):
    mlflow=pytest.importorskip('mlflow')
    from mobyai.models.config import TracingConfig
    import mobyai.tracing as tracing
    from mobyai.engine.planning import plan_project
    from tests.unit.test_planner import _agent, VALID_PLAN
    from mobyai.state.memory import InMemoryStateStore
    monkeypatch.delenv('MLFLOW_TRACKING_URI',raising=False)
    monkeypatch.setattr(tracing,'_mlflow',None)
    tracing.configure(TracingConfig(enabled=True,experiment='minutes-test'),tmp_path/'state.db')
    try:
        store=InMemoryStateStore();workspace=Workspace(tmp_path/'code')
        project=make_project(workspace_root=workspace.root)
        await store.projects.save(project)
        _,agent=_agent([json.dumps(VALID_PLAN)])
        await plan_project(project,store,agent,workspace=workspace)
        tracing.flush()
        record=minutes.plan_records(project)[0]
        assert record['mlflow_status']=='captured'
        assert {'planner.plan','model.request'}.issubset({s['name'] for s in record['mlflow_spans']})
        assert all(s['end_time_unix_nano'] for s in record['mlflow_spans'])
    finally:
        tracing.configure(TracingConfig(),tmp_path/'state.db')

async def test_completed_report_failure_preserves_completion(tmp_path,monkeypatch):
    from mobyai.review.service import requested_report
    _,runtime=_runtime(tmp_path)
    project=make_project(workspace_root=tmp_path,state=ProjectState.COMPLETED)
    await runtime.store.projects.save(project)
    monkeypatch.setattr('mobyai.review.service.logic_report',AsyncMock(side_effect=ValueError('model offline')))
    await requested_report(runtime,project,completed=True)
    assert (await runtime.store.projects.get(project.project_id)).state==ProjectState.COMPLETED
    events=await runtime.store.events.list_for_project(project.project_id)
    assert events[-1].event=='logic_report_failed'

async def test_approval_does_not_cover_subsequent_source_changes_before_execution(tmp_path,monkeypatch):
    runtime,project,workspace,_=await existing(tmp_path,monkeypatch)
    await _execute_project_inner(runtime,project,io.StringIO(),max_steps=None)
    proposal=minutes.read(minutes.run_dir(project)/'proposal.json')
    await approve(runtime,project,proposal['revision'])
    (workspace/'package-lock.json').write_text('{"changed":true}')
    with pytest.raises(ValueError,match='changed after approval'):
        await ensure_approved(runtime.store,project)

async def test_archived_plan_history_can_generate_report_without_old_database_events(tmp_path,monkeypatch):
    runtime,project,workspace,review=await existing(tmp_path,monkeypatch,native=True)
    minutes.write(minutes.run_dir(project)/'plan-legacy.json',{'id':'plan-legacy','project_id':'removed-old-project',
        'prompt':'Original request','plan':{'summary':'Original plan'},'calls':[],'status':'ok'})
    result=await logic_report(runtime,project)
    assert len(result['reports'])==1
    assert result['reports'][0]['plan_id']=='plan-legacy'
    assert (workspace/'mobyMinutes'/'reasoning-logic-report.md').is_file()
    assert review.call_args.args[0]['original_prompt']=='Original request'


def test_shared_workspace_lease_blocks_different_project_ids(tmp_path):
    from mobyai.engine.lease import workspace_lease, ProjectBusyError
    database=tmp_path/'state.db'
    with workspace_lease(database,tmp_path):
        with pytest.raises(ProjectBusyError):
            with workspace_lease(database,tmp_path): pass

async def test_reviewer_uses_read_only_neighbor_queries_and_supplied_schema():
    from mobyai.review.agents import ReviewerAgent
    from mobyai.inference.fake import FakeInferenceProvider
    from mobyai.inference.structured import StructuredGenerator
    provider=FakeInferenceProvider([
        json.dumps({'queries':['greet entry']}),
        json.dumps({'report':{'summary':'Greeting entry point','findings':[{'claim':'greet returns a string','evidence':['app.py:1:greet']}],'limitations':[],'recommendations':[]}})
    ])
    calls=[]
    def retrieve(query):
        calls.append(query)
        return [{'id':'app.py:1:greet','body':'def greet(): return "hi"','retrieval':'graph_neighbor'}]
    report=await ReviewerAgent(StructuredGenerator(provider),'code_reviewer').review({'evidence_ids':[]},retrieve)
    assert calls==['greet entry']
    assert report.findings[0].evidence==['app.py:1:greet']

async def test_logic_report_compares_updates_to_previous_tasks_and_filters_outcomes(tmp_path,monkeypatch):
    runtime,project,workspace,review=await existing(tmp_path,monkeypatch,native=True)
    base=minutes.run_dir(project)
    minutes.write(base/'plan-a.json',{'id':'a','project_id':project.project_id,'started_at':'2026-01-01T00:00:00+00:00',
        'finished_at':'2026-01-01T00:00:01+00:00','status':'ok','plan':{'tasks':[{'task_id':'OLD','description':'old contract'}]}})
    minutes.write(base/'plan-b.json',{'id':'b','project_id':project.project_id,'started_at':'2026-01-02T00:00:00+00:00',
        'finished_at':'2026-01-02T00:00:01+00:00','status':'ok','plan':{'supersede':[{'task_id':'OLD','replacements':[{'task_id':'NEW','description':'fixed contract'}]}]}})
    minutes.write(base/'outcome-test.json',{'task_id':'NEW','timestamp':'2026-01-03T00:00:00+00:00','passed':True,'tests':[]})
    await logic_report(runtime,project)
    first,second=review.call_args_list
    assert first.args[0]['outcomes']==[]
    assert second.args[0]['replaced_tasks_before_update']['OLD']['description']=='old contract'
    assert second.args[0]['outcomes'][0]['passed'] is True

@pytest.mark.parametrize('native',[False,True])
async def test_existing_workflow_executes_only_after_approval_and_generates_completion_report(tmp_path,monkeypatch,native):
    from mobyai.engine.state_machine import ProjectStateMachine
    from mobyai.inference.fake import FakeInferenceProvider
    from tests.unit.test_cli import WRITE_APP
    runtime,project,workspace,review=await existing(tmp_path,monkeypatch,native=native)
    before=(workspace/'main.py').read_bytes()
    runtime.worker._generator.provider=FakeInferenceProvider([json.dumps(WRITE_APP)])
    await _execute_project_inner(runtime,project,io.StringIO(),max_steps=None)
    assert not (workspace/'src'/'app.py').exists()
    proposal=minutes.read(minutes.run_dir(project)/'proposal.json')
    await approve(runtime,project,proposal['revision'])
    current=ProjectStateMachine.resume(await runtime.store.projects.get(project.project_id))
    await runtime.store.projects.save(current)
    assert await _execute_project_inner(runtime,current,io.StringIO(),max_steps=None)==0
    assert (await runtime.store.projects.get(project.project_id)).state==ProjectState.COMPLETED
    assert (workspace/'main.py').read_bytes()==before
    assert (workspace/'src'/'app.py').exists()
    assert (workspace/'mobyMinutes'/'reasoning-logic-report.md').exists()
    assert list(minutes.run_dir(project).glob('outcome-*.json'))

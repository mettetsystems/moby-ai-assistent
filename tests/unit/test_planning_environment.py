import json
from mobyai.engine.environment import snapshot
from mobyai.engine.planning import plan_project
from mobyai.engine.replanning import replan_project
from mobyai.execution.workspace import Workspace
from mobyai.agents.planner import PlannerAgent
from mobyai.inference.fake import FakeInferenceProvider
from mobyai.inference.structured import StructuredGenerator
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import make_project
from tests.unit.test_cli import PLAN
from tests.unit.test_replan import _escalated_store,_planner,_patch_payload


def test_snapshot_reports_tools_without_executing_them(tmp_path,monkeypatch):
    monkeypatch.setattr('mobyai.engine.environment.shutil.which',lambda name:'/usr/bin/'+name if name in {'podman','podman-compose'} else None)
    monkeypatch.setenv('HF_TOKEN','not-for-the-planner')
    (tmp_path/'package.json').write_text('{}')
    data=snapshot(Workspace(tmp_path))
    assert data['executables_on_runner_path']['docker-compose'] is None
    assert data['executables_on_runner_path']['podman-compose']=='/usr/bin/podman-compose'
    assert data['root_manifests']==['package.json']
    assert 'not-for-the-planner' not in json.dumps(data)


async def test_initial_plan_receives_environment(tmp_path):
    store=InMemoryStateStore();project=make_project(workspace_root=tmp_path)
    await store.projects.save(project)
    provider=FakeInferenceProvider([json.dumps(PLAN)])
    await plan_project(project,store,PlannerAgent(StructuredGenerator(provider)),workspace=Workspace(tmp_path))
    assert 'HOST ENVIRONMENT PREFLIGHT' in provider.requests[0].messages[1].content
    assert any(e.event=='environment_preflight' for e in await store.events.list_for_project(project.project_id))


async def test_replan_receives_fresh_environment(tmp_path):
    store,workspace=await _escalated_store(tmp_path)
    provider,agent=_planner([json.dumps(_patch_payload())])
    await replan_project(await store.projects.get('proj-1'),store,agent,workspace=workspace)
    assert 'HOST ENVIRONMENT PREFLIGHT' in provider.requests[0].messages[1].content

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sys

import pytest

from mobyai.console.workloads import Workloads
from mobyai.engine.stopping import pause_interrupted
from mobyai.engine.orchestration import run_project, OrchestrationStep
from mobyai.execution.commands import CommandRunner
from mobyai.execution.diagnostics import diagnose
from mobyai.execution.workspace import Workspace
from mobyai.inference.recovery import recovery_stage, current_stage
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.config import RoleModelConfig
from mobyai.models.inference import ModelRequest
from mobyai.models.project import Project, ProjectState
from mobyai.models.task import Task, TaskStatus
from mobyai.models.attempt import TaskAttempt, AttemptResult
from mobyai.models.event import EventRecord
from mobyai.models.orchestrator import OrchestratorDecision
from tests.unit.test_cli import _runtime


def project(tmp_path, state=ProjectState.WORKING):
    return Project(project_id='demo', name='Demo', objective='Build', workspace_root=tmp_path,
                   state=state, current_task_id='STEP_1', created_at=datetime.now(UTC), updated_at=datetime.now(UTC))


async def test_stop_requires_project_bound_expiring_confirmation(tmp_path, monkeypatch):
    path, runtime = _runtime(tmp_path)
    workloads = Workloads(runtime, path)
    workloads.project_id = 'demo'
    workloads.process = SimpleNamespace(pid=1234, returncode=None)
    terminate = AsyncMock()
    monkeypatch.setattr(workloads, '_stop_current', terminate)
    with pytest.raises(ValueError):
        await workloads.stop({'project_id':'demo'})
    challenge = await workloads.prepare_stop({'project_id':'demo'})
    payload = {**challenge, 'confirmation':'wrong'}
    with pytest.raises(ValueError):
        await workloads.stop(payload)
    terminate.assert_not_called()
    payload['confirmation'] = 'demo'
    workloads.stop_challenge = (*workloads.stop_challenge[:3], 0)
    with pytest.raises(ValueError):
        await workloads.stop(payload)
    challenge = await workloads.prepare_stop({'project_id':'demo'})
    payload = {**challenge, 'confirmation':'demo'}
    assert (await workloads.stop(payload))['status'] == 'stopping'
    with pytest.raises(ValueError):
        await workloads.stop(payload)
    await workloads.stop_task
    terminate.assert_awaited_once()


async def test_pause_closes_attempt_and_preserves_completed_work(tmp_path):
    _, runtime = _runtime(tmp_path)
    item = project(tmp_path)
    await runtime.store.projects.save(item)
    await runtime.store.tasks.save('demo', Task(task_id='STEP_1', name='Work', description='Work', status=TaskStatus.RUNNING, attempt_count=1))
    await runtime.store.tasks.save('demo', Task(task_id='DONE', name='Done', description='Done', status=TaskStatus.PASSED))
    await runtime.store.attempts.save(TaskAttempt(project_id='demo', task_id='STEP_1', attempt_number=1, started_at=datetime.now(UTC)))
    paused = await pause_interrupted(runtime.store, 'demo')
    assert paused.state == ProjectState.PAUSED
    assert paused.paused_from == ProjectState.READY
    assert (await runtime.store.tasks.get('demo','STEP_1')).status == TaskStatus.READY
    assert (await runtime.store.tasks.get('demo','DONE')).status == TaskStatus.PASSED
    assert (await runtime.store.attempts.get('demo','STEP_1',1)).result == AttemptResult.ERROR
    assert await pause_interrupted(runtime.store, 'demo') == paused


async def test_cancel_command_stops_its_process(tmp_path):
    runner = CommandRunner(Workspace(tmp_path))
    pid_file = tmp_path/'pid'
    task = asyncio.create_task(runner.run([sys.executable, '-c',
        "import os,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"]))
    try:
        async with asyncio.timeout(5):
            while not pid_file.exists():
                await asyncio.sleep(.02)
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        import os
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        task.cancel()


async def test_missing_executable_is_diagnostic_not_crash(tmp_path):
    result = await CommandRunner(Workspace(tmp_path)).run(['mobyai-nonexistent-tool-xyz'])
    assert result.exit_code == 127
    assert diagnose(result.stderr).category == 'toolchain'


@pytest.mark.parametrize('error,category,eligible', [
    ('npm error Missing script: "test:bpm"','npm_missing_script',True),
    ('npm ERR! ERESOLVE unable to resolve dependency tree','npm_dependency_conflict',True),
    ('npm ERR! EAI_AGAIN registry.npmjs.org','network',False),
    ('npm ERR! EACCES','access',False),
    ('cudaMalloc out of memory','capacity',False),
])
def test_failure_classification(error, category, eligible):
    result = diagnose(error)
    assert result.category == category
    assert result.sampling_eligible == eligible


def test_sampling_is_bounded_local_and_can_be_disabled():
    config = RoleModelConfig(model='test', endpoint='http://localhost', sampling={'temperature':0.1})
    gen = StructuredGenerator(SimpleNamespace(), settings=config)
    request = ModelRequest(messages=[{"role":"user","content":"test"}])
    for stage, expected in [(0,.1),(1,.1),(2,.1),(3,.2),(4,.2),(5,.2),(6,.3),(17,.6),(99,.6)]:
        with recovery_stage(stage):
            assert gen._with_live_settings(request).temperature == expected
    assert config.sampling.temperature == .1
    assert current_stage() == 0
    config.recovery_sampling = []
    with recovery_stage(99):
        assert gen._with_live_settings(request).temperature == .1


async def test_post_replan_failure_escalates_and_success_resets(tmp_path, monkeypatch):
    import mobyai.engine.orchestration as engine
    _, runtime = _runtime(tmp_path)
    item = project(tmp_path, ProjectState.READY)
    await runtime.store.projects.save(item)
    await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC), project_id='demo', component='planner', event='dag_replanned'))
    seen = []
    async def advance(current, *args, **kwargs):
        seen.append(current_stage())
        passed = len(seen) == 2
        state = ProjectState.COMPLETED if passed else ProjectState.RETRYING
        return OrchestrationStep(project=item.model_copy(update={'state':state,'last_error':None if passed else 'test failed'}),
            decision=OrchestratorDecision.CONTINUE, execution=SimpleNamespace(passed=passed))
    monkeypatch.setattr(engine, 'advance_project', advance)
    await run_project(item, runtime.store, None, None, Workspace(tmp_path))
    assert seen == [0, 1]
    events = await runtime.store.events.list_for_project('demo')
    assert [e.payload['stage'] for e in events if e.event == 'sampling_recovery'] == [1, 0]
    assert current_stage() == 0


async def test_legacy_replan_budget_does_not_stop_recovery(tmp_path,monkeypatch):
    import mobyai.engine.orchestration as engine
    _, runtime = _runtime(tmp_path)
    item = project(tmp_path, ProjectState.ESCALATING)
    await runtime.store.projects.save(item)
    for _ in range(25):
        await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC), project_id='demo', component='planner', event='dag_replanned'))
    async def replan(current,*args,**kwargs):
        ready=current.model_copy(update={'state':ProjectState.READY})
        await runtime.store.projects.save(ready)
        return SimpleNamespace(project=ready)
    async def advance(current,*args,**kwargs):
        done=current.model_copy(update={'state':ProjectState.COMPLETED})
        return OrchestrationStep(project=done,decision=OrchestratorDecision.MISSION_COMPLETE)
    monkeypatch.setattr(engine,'replan_project',replan)
    monkeypatch.setattr(engine,'advance_project',advance)
    result = await run_project(item, runtime.store, None, None, Workspace(tmp_path), planner=object(), max_replans=1)
    assert result.project.state == ProjectState.COMPLETED


async def test_transient_inference_retries_without_changing_sampling(monkeypatch):
    import httpx
    from mobyai.inference.openai_compatible import OpenAICompatibleProvider
    from mobyai.inference.base import InferenceError
    seen = []
    def handler(request):
        seen.append(request.content)
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}]})
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider('http://localhost', 'test', client=client)
        request = ModelRequest(messages=[{'role':'user','content':'test'}])
        assert (await provider.generate(request)).content == 'ok'
        assert seen[0] == seen[1]
    seen.clear()
    def bad(request):
        seen.append(request)
        return httpx.Response(400, text='bad request')
    async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
        with pytest.raises(InferenceError):
            await OpenAICompatibleProvider('http://localhost','test',client=client).generate(request)
        assert len(seen) == 1


def test_sampling_changes_one_setting_per_stage_from_custom_baseline():
    from mobyai.inference.recovery import sampling_steps
    config = RoleModelConfig(model='test', endpoint='http://localhost',
                             sampling={'temperature': 0.05, 'top_p': 1.0, 'top_k': None})
    original = config.sampling.model_dump()
    gen = StructuredGenerator(SimpleNamespace(), settings=config)
    request = ModelRequest(messages=[{"role": "user", "content": "test"}])
    previous = gen._with_live_settings(request)
    changes = []
    for stage in range(1, 19):
        with recovery_stage(stage):
            current = gen._with_live_settings(request)
        changed = [key for key in ('temperature', 'top_k', 'top_p')
                   if getattr(previous, key) != getattr(current, key)]
        assert len(changed) == 1
        changes.extend(changed)
        previous = current
    assert changes == ['temperature', 'top_k', 'top_p'] * 6
    assert config.sampling.model_dump() == original
    assert len(sampling_steps(config.sampling, [config.sampling])) == 0
    with recovery_stage(100):
        assert gen._with_live_settings(request) == previous
    assert gen._with_live_settings(request).temperature == 0.05


def test_orchestrator_does_not_change_sampling():
    config = RoleModelConfig(model='test', endpoint='http://localhost')
    gen = StructuredGenerator(SimpleNamespace(), settings=config, recovery_enabled=False)
    request = ModelRequest(messages=[{"role":"user", "content":"test"}])
    with recovery_stage(18):
        actual = gen._with_live_settings(request)
    assert (actual.temperature, actual.top_k, actual.top_p) == (0.0, None, 1.0)


async def test_coordinate_stage_continues_past_six_and_survives_resume(tmp_path, monkeypatch):
    import mobyai.engine.orchestration as engine
    _, runtime = _runtime(tmp_path)
    item = project(tmp_path, ProjectState.RETRYING)
    await runtime.store.projects.save(item)
    await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC), project_id='demo', component='planner', event='dag_replanned'))
    await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC), project_id='demo', component='runtime', event='sampling_recovery', payload={'stage':6}))
    seen = []
    async def advance(current, *args, **kwargs):
        seen.append(current_stage())
        return OrchestrationStep(project=item.model_copy(update={'last_error':'test failed'}),
            decision=OrchestratorDecision.CONTINUE, execution=SimpleNamespace(passed=False))
    monkeypatch.setattr(engine, 'advance_project', advance)
    for _ in range(2):
        result = await run_project(item, runtime.store, None, None, Workspace(tmp_path), max_steps=1)
        assert result.project.state == ProjectState.PAUSED
    assert seen == [6, 7]

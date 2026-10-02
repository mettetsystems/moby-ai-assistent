"""Durable evidence contracts and execution integration, with no live models."""
import asyncio
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from mobyai.models.records import RunRecord, StepRunRecord, PlanRecord
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.project import ProjectState
from mobyai.records.capture import Recorder, operation, begin_step, emit
from mobyai.records.export import export_records
from mobyai.state.memory import InMemoryStateStore
from mobyai.state.sqlite import SqliteStateStore
from tests.helpers import FROZEN_NOW, make_project, make_task


@pytest.fixture(params=['memory', 'sqlite'])
async def store(request, tmp_path):
    value = InMemoryStateStore() if request.param == 'memory' else SqliteStateStore(tmp_path / 'state.db')
    yield value
    if isinstance(value, SqliteStateStore):
        await value.close()


async def payload(store, entry):
    return json.loads(await store.records.read_artifact(entry.payload))


async def ready(store, tmp_path):
    from tests.unit.test_orchestrator import _exists_command
    from mobyai.execution.workspace import Workspace
    project = make_project(workspace_root=tmp_path / 'workspace')
    workspace = Workspace(project.workspace_root)
    workspace.ensure_root()
    await store.projects.save(project)
    await store.tasks.save(project.project_id, make_task('STEP_001', status='ready',
        target_files=['src/app.py'], test_commands=[_exists_command('src/app.py')]))
    project = project.model_copy(update={'state': ProjectState.READY})
    await store.projects.save(project)
    return project, workspace


async def test_retry_captures_each_turn_and_validation(store, tmp_path):
    from tests.unit.test_orchestrator import _worker, _orchestrator, _retry_payload, _write_payload, _empty_payload
    from mobyai.engine.orchestration import advance_project
    project, workspace = await ready(store, tmp_path)
    first_turn = {'summary': 'Inspect', 'done': False, 'changed_files': [],
                  'tool_calls': [{'name': 'list_directory', 'arguments': {'path': '.'}}]}
    _, worker = _worker([json.dumps(first_turn), json.dumps(_empty_payload()),
                         json.dumps(_write_payload('src/app.py', 'print("héllo")\n'))])
    _, orchestrator = _orchestrator([json.dumps(_retry_payload())])
    first = await advance_project(project, store, worker, orchestrator, workspace)
    second = await advance_project(first.project, store, worker, orchestrator, workspace)
    assert second.execution.passed
    bundle = await store.records.snapshot(project.project_id)
    assert bundle.run.completeness == 'complete'
    assert [s.outcome for s in bundle.steps] == ['fail', 'pass']
    failed, passed = bundle.steps
    assert failed.id != passed.id
    assert len([e for e in failed.history if e.kind == 'worker_turn']) == 2
    assert any(e.kind == 'tool_started' for e in failed.history)
    assert any(e.kind == 'command_finished' for e in failed.history)
    assert any(e.kind == 'validation_finished' for e in passed.history)
    request = json.loads(await store.records.read_artifact(failed.initial_prompt))
    assert request['request']['messages'][0]['role'] == 'system'
    changed = [await payload(store, e) for e in passed.history if e.kind == 'tool_started']
    assert changed[0]['call']['arguments']['contents'] == 'print("héllo")\n'
    all_entries = [*bundle.run.history, *bundle.plan.history, *[e for s in bundle.steps for e in s.history if e.scope == 'step']]
    sequences = [e.sequence for e in all_entries]
    assert len(sequences) == len(set(sequences))
    dest = tmp_path / 'export'
    manifest = await export_records(store, project.project_id, dest)
    assert manifest['completeness'] == 'complete'
    RunRecord.model_validate_json((dest / 'run.json').read_text())
    PlanRecord.model_validate_json((dest / 'plan.json').read_text())
    for path in (dest / 'steps').glob('*.json'):
        StepRunRecord.model_validate_json(path.read_text())
    for ref in bundle.artifacts:
        assert (dest / ref.path).is_file()
    with pytest.raises(FileExistsError):
        await export_records(store, project.project_id, dest)


async def test_planning_captures_rejections_and_applied_revision(store, tmp_path):
    from tests.unit.test_planner import VALID_PLAN, CYCLIC_PLAN
    from mobyai.agents.planner import PlannerAgent
    from mobyai.engine.planning import plan_project
    from mobyai.inference.fake import FakeInferenceProvider
    from mobyai.inference.structured import StructuredGenerator
    project = make_project(workspace_root=tmp_path)
    await store.projects.save(project)
    provider = FakeInferenceProvider(['not json', json.dumps({'plan': CYCLIC_PLAN}), json.dumps({'plan': VALID_PLAN})])
    agent = PlannerAgent(StructuredGenerator(provider))
    await plan_project(project, store, agent, scaffold=False)
    bundle = await store.records.snapshot(project.project_id)
    kinds = [e.kind for e in bundle.plan.history]
    assert 'schema_rejected' in kinds
    assert 'plan_rejected' in kinds
    assert 'plan_validated' in kinds
    assert 'plan_applied' in kinds
    assert bundle.plan.active_revision_id == next(e.id for e in bundle.plan.history if e.kind == 'plan_applied')
    assert not any(e.kind == 'event:review_plan_approved' for e in bundle.plan.history)
    raw = [await payload(store, e) for e in bundle.plan.history if e.kind == 'schema_rejected']
    assert raw[0]['raw'] == 'not json'


async def test_capture_failure_does_not_fail_worker(store, tmp_path, monkeypatch, caplog):
    from tests.unit.test_worker import _agent, _write_app_payload
    from mobyai.engine.working import execute_task
    project, workspace = await ready(store, tmp_path)
    original = store.records.append
    async def fail(*args, **kwargs):
        raise OSError('capture disk unavailable')
    monkeypatch.setattr(store.records, 'append', fail)
    _, agent = _agent([json.dumps(_write_app_payload())])
    result = await execute_task(project, 'STEP_001', store, agent, workspace)
    assert result.passed
    assert 'Research capture incomplete' in caplog.text
    monkeypatch.setattr(store.records, 'append', original)
    await Recorder(store, result.project, 'harness').emit('capture_recovered', {})
    bundle = await store.records.snapshot(project.project_id)
    assert bundle.run.completeness == 'partial'
    assert bundle.steps[0].outcome == 'pass'
    assert any('unavailable' in warning for warning in bundle.run.warnings)


async def test_storage_reopen_resume_preserves_ids_and_unknown_actions(tmp_path):
    path = tmp_path / 'state.db'
    project = make_project(workspace_root=tmp_path / 'workspace')
    task = make_task('STEP_001')
    attempt = TaskAttempt(project_id=project.project_id, task_id=task.task_id, attempt_number=1, started_at=FROZEN_NOW)
    async with SqliteStateStore(path) as store:
        await store.projects.save(project)
        await store.tasks.save(project.project_id, task)
        attempt = await store.attempts.save(attempt)
        recorder = Recorder(store, project, 'worker')
        await recorder.start()
        await recorder.begin_step(task, attempt)
        await recorder.emit('tool_started', {'call': {'name': 'write_file'}}, correlation_id=uuid4())
        before = await store.records.snapshot(project.project_id)
    async with SqliteStateStore(path) as store:
        recorder = Recorder(store, project, 'worker')
        await recorder.start()
        await recorder.begin_step(task, attempt)
        after = await store.records.snapshot(project.project_id)
        assert before.run.id == after.run.id
        assert before.steps[0].id == after.steps[0].id
        assert len(after.steps) == 1
        assert after.steps[0].completeness == 'unknown'
        assert len([e for e in after.steps[0].history if e.kind == 'step_segment_started']) == 2
        assert not any(e.kind == 'tool_finished' for e in after.steps[0].history)


async def test_interrupted_step_is_not_success(store, tmp_path):
    project = make_project(workspace_root=tmp_path)
    await store.projects.save(project)
    task = make_task('STEP_001')
    await store.tasks.save(project.project_id, task)
    attempt = await store.attempts.save(TaskAttempt(project_id=project.project_id, task_id=task.task_id,
                                                   attempt_number=1, started_at=FROZEN_NOW))
    @operation('worker')
    async def interrupted(project, store):
        await begin_step(task, attempt)
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await interrupted(project, store)
    bundle = await store.records.snapshot(project.project_id)
    assert bundle.steps[0].outcome == 'interrupted'
    await store.attempts.save(attempt.model_copy(update={'result': AttemptResult.ERROR, 'finished_at': FROZEN_NOW}))
    assert (await store.records.snapshot(project.project_id)).steps[0].outcome == 'interrupted'


async def test_export_missing_and_corrupt_artifacts(tmp_path):
    async with SqliteStateStore(tmp_path / 'records.db') as store:
        project = make_project(workspace_root=tmp_path)
        await store.projects.save(project)
        bundle = await store.records.snapshot(project.project_id)
        refs = bundle.artifacts
        store.records._path(refs[0].sha256).unlink()
        store.records._path(refs[1].sha256).write_bytes(b'corrupt')
        manifest = await export_records(store, project.project_id, tmp_path / 'export')
        assert manifest['completeness'] == 'partial'
        assert len(manifest['unavailable_artifacts']) == 2
        assert RunRecord.model_validate_json((tmp_path / 'export/run.json').read_text()).completeness == 'partial'


async def test_delete_preserves_shared_artifacts_and_export(store, tmp_path):
    a = make_project(workspace_root=tmp_path)
    b = make_project(project_id='other', workspace_root=tmp_path)
    await store.projects.save(a)
    await store.projects.save(b)
    before = await store.records.snapshot(b.project_id)
    await export_records(store, a.project_id, tmp_path / 'saved')
    await store.delete_project(a.project_id)
    with pytest.raises(LookupError):
        await store.records.snapshot(a.project_id)
    for ref in before.artifacts:
        assert await store.records.read_artifact(ref)
    assert (tmp_path / 'saved/run.json').is_file()


async def test_old_project_is_partial_without_invented_steps(store, tmp_path):
    project = make_project(workspace_root=tmp_path, state='READY')
    await store.projects.save(project)
    await store.tasks.save(project.project_id, make_task('STEP_001'))
    await store.attempts.save(TaskAttempt(project_id=project.project_id, task_id='STEP_001', attempt_number=1,
                                        started_at=FROZEN_NOW, result='fail'))
    await Recorder(store, project, 'harness').start()
    bundle = await store.records.snapshot(project.project_id)
    assert bundle.run.completeness == 'partial'
    assert bundle.steps == []
    assert any('not reconstructed' in w for w in bundle.run.warnings)


async def test_prompt_snapshots_are_not_rewritten(store, tmp_path):
    from mobyai.agents.prompts.settings import PromptSnapshot
    from mobyai.models.config import AppConfig
    from tests.unit.test_worker import _agent, _write_app_payload
    from mobyai.engine.working import execute_task
    project, workspace = await ready(store, tmp_path)
    config = AppConfig.from_yaml(Path('config/default.yaml'))
    snapshots = PromptSnapshot(config)
    _, agent = _agent([json.dumps(_write_app_payload()), json.dumps(_write_app_payload())])
    agent.prompts = snapshots
    config.system_prompts.worker = 'ORIGINAL WORKER PROMPT'
    await execute_task(project, 'STEP_001', store, agent, workspace)
    before = await store.records.snapshot(project.project_id)
    original = await store.records.read_artifact(before.steps[0].initial_prompt)
    config.system_prompts.worker = 'EDITED WORKER PROMPT'
    snapshots.begin_attempt()
    from tests.unit.test_orchestrator import _exists_command
    await store.tasks.save(project.project_id, make_task('STEP_002', status='ready',
        target_files=['src/app.py'], test_commands=[_exists_command('src/app.py')]))
    await execute_task(await store.projects.get(project.project_id), 'STEP_002', store, agent, workspace)
    after = await store.records.snapshot(project.project_id)
    assert b'EDITED WORKER PROMPT' in await store.records.read_artifact(after.steps[1].initial_prompt)
    assert b'ORIGINAL WORKER PROMPT' in original
    assert await store.records.read_artifact(before.steps[0].initial_prompt) == original


async def test_cli_exports_without_inference(store, tmp_path):
    from types import SimpleNamespace
    from mobyai.cli import async_main
    project = make_project(workspace_root=tmp_path)
    await store.projects.save(project)
    async def initialize():
        pass
    runtime = SimpleNamespace(store=store, initialize=initialize)
    stdout = io.StringIO()
    code = await async_main(['records', 'export', project.project_id, '--output', str(tmp_path / 'bundle')],
                            runtime=runtime, stdout=stdout)
    assert code == 0
    assert 'Records exported' in stdout.getvalue()
    assert (tmp_path / 'bundle/manifest.json').exists()


async def test_human_edits_and_approvals_preserve_revision_history(tmp_path, monkeypatch):
    from tests.unit.test_plan_editing import prepared
    from mobyai.review import editing
    from mobyai.review.service import approve
    runtime, project, _ = await prepared(tmp_path, monkeypatch)
    try:
        before = await runtime.store.records.snapshot(project.project_id)
        draft = await editing.snapshot(runtime.store, project)
        draft['plan']['tasks'][0]['description'] = 'Preserve behavior and expand coverage'
        saved = await editing.save(runtime, project, {**draft, 'reason': 'Clarify validation'})
        await approve(runtime, project, saved['revision'])
        after = await runtime.store.records.snapshot(project.project_id)
        assert before.run.id == after.run.id
        applied = [e for e in after.plan.history if e.kind == 'plan_applied']
        assert len(applied) >= 2
        assert applied[-1].actor == 'human'
        assert applied[-1].parent_revision_id == applied[-2].id
        assert applied[-1].reason == 'Clarify validation'
        approval = next(e for e in after.plan.history if e.kind == 'event:review_plan_approved')
        assert approval.parent_revision_id == applied[-1].id
    finally:
        await runtime.aclose()


async def test_replan_retains_full_patch_and_plan(tmp_path):
    from tests.unit.test_replan import _escalated_store, _planner, _patch_payload
    from mobyai.engine.replanning import replan_project
    store, workspace = await _escalated_store(tmp_path)
    _, agent = _planner([json.dumps(_patch_payload())])
    await replan_project(await store.projects.get('proj-1'), store, agent, workspace=workspace)
    bundle = await store.records.snapshot('proj-1')
    entry = next(e for e in bundle.plan.history if e.kind == 'plan_applied')
    data = await payload(store, entry)
    assert data['proposal']['supersede']
    assert any(t['task_id'] == 'STEP_001B' for t in data['tasks'])
    assert bundle.run.completeness == 'partial'  # Existing project, capture forward only.


async def test_final_pass_plan_is_recorded(tmp_path, monkeypatch):
    from tests.unit.test_final_pass import setup
    from mobyai.final_pass.service import final_pass
    runtime, project, _ = await setup(tmp_path, monkeypatch)
    try:
        await final_pass(runtime, project)
        bundle = await runtime.store.records.snapshot(project.project_id)
        entry = next(e for e in bundle.plan.history if e.kind == 'plan_applied')
        data = await payload(runtime.store, entry)
        assert data['phase'] == 'final_fail'
        assert data['proposal']['tasks']
    finally:
        await runtime.aclose()


async def test_migration_from_v3_keeps_old_state(tmp_path):
    import sqlite3
    from mobyai.state.migrations import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3
    path = tmp_path / 'old.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version, sql in enumerate((SCHEMA_V1, SCHEMA_V2, SCHEMA_V3), 1):
        conn.executescript(sql)
        conn.execute('INSERT INTO schema_migrations VALUES (?, ?)', (version, FROZEN_NOW.isoformat()))
        conn.commit()
    conn.close()
    async with SqliteStateStore(path) as store:
        await store.projects.save(make_project())
        bundle = await store.records.snapshot('proj-1')
        assert bundle.run.state == 'NEW'
    conn = sqlite3.connect(path)
    assert [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4]
    conn.close()


async def test_partial_capture_survives_restart_after_recovery(tmp_path, monkeypatch):
    path = tmp_path / 'durable.db'
    async with SqliteStateStore(path) as store:
        project = make_project(workspace_root=tmp_path)
        await store.projects.save(project)
        original = store.records.append
        async def fail(*args, **kwargs):
            raise OSError('temporarily unavailable')
        monkeypatch.setattr(store.records, 'append', fail)
        await Recorder(store, project, 'worker').emit('model_response', {'response': 'lost'})
        monkeypatch.setattr(store.records, 'append', original)
        await Recorder(store, project, 'worker').emit('capture_recovered', {})
    async with SqliteStateStore(path) as store:
        bundle = await store.records.snapshot(project.project_id)
        assert bundle.run.completeness == bundle.plan.completeness == 'partial'
        assert 'temporarily unavailable' in bundle.run.warnings[0]


async def test_symlink_export_is_rejected(store, tmp_path):
    project = make_project(workspace_root=tmp_path)
    await store.projects.save(project)
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        await export_records(store, project.project_id, link / 'export')
    assert not (real / 'export').exists()


@pytest.mark.parametrize('linked_root', [False, True])
async def test_capture_through_symlinked_storage_parent(tmp_path, linked_root):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    artifact_root = real / 'state.db.records'
    if linked_root:
        artifact_root = tmp_path / 'artifacts'
        artifact_root.mkdir()
        (real / 'state.db.records').symlink_to(artifact_root, target_is_directory=True)
    project = make_project(workspace_root=tmp_path / 'workspace')
    async with SqliteStateStore(link / 'state.db') as store:
        await store.projects.save(project)
        bundle = await store.records.snapshot(project.project_id)
        assert bundle.run.completeness == 'complete'
        assert store.records.artifact_root == artifact_root
        for ref in bundle.artifacts:
            assert await store.records.read_artifact(ref)
        await export_records(store, project.project_id, tmp_path / 'export')
    async with SqliteStateStore(link / 'state.db') as store:
        reopened = await store.records.snapshot(project.project_id)
        assert reopened.run.id == bundle.run.id
        await store.delete_project(project.project_id)
        assert not list((real / 'state.db.records').glob('*/*'))


@pytest.mark.parametrize('component', ['root', 'shard', 'blob'])
async def test_artifact_symlinks_introduced_after_initialization_are_rejected(tmp_path, component):
    async with SqliteStateStore(tmp_path / 'state.db') as store:
        project = make_project(workspace_root=tmp_path)
        await store.projects.save(project)
        bundle = await store.records.snapshot(project.project_id)
        ref = bundle.artifacts[0]
        body = json.loads(await store.records.read_artifact(ref))
        blob = store.records._path(ref.sha256)
        target = {'root': store.records.artifact_root, 'shard': blob.parent, 'blob': blob}[component]
        moved = tmp_path / 'moved'
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=component != 'blob')
        with pytest.raises(ValueError, match='symlinked record artifact'):
            await store.records.read_artifact(ref)
        with pytest.raises(ValueError, match='symlinked record artifact'):
            await store.records.append(project.project_id, scope='run', kind='test',
                                       actor='harness', payload=body)


async def test_failed_run_initialization_reports_missing_run_without_type_error(store, tmp_path, monkeypatch, caplog):
    project = make_project(workspace_root=tmp_path)
    task = make_task('STEP_001')
    attempt = TaskAttempt(project_id=project.project_id, task_id=task.task_id,
                          attempt_number=1, started_at=FROZEN_NOW)
    original = store.records.ensure_run

    async def fail(*args, **kwargs):
        raise OSError('capture disk unavailable')

    monkeypatch.setattr(store.records, 'ensure_run', fail)
    recorder = Recorder(store, project, 'worker')
    await recorder.start()
    await recorder.begin_step(task, attempt)
    assert recorder.step_id is None
    assert 'No captured run for project' in caplog.text
    assert 'TypeError' not in caplog.text

    monkeypatch.setattr(store.records, 'ensure_run', original)
    await recorder.start()
    await recorder.begin_step(task, attempt)
    bundle = await store.records.snapshot(project.project_id)
    assert len(bundle.steps) == 1
    assert bundle.run.completeness == 'partial'
    assert any('capture disk unavailable' in warning for warning in bundle.run.warnings)

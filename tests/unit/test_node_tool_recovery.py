import json
import sys
import pytest
from mobyai.execution.commands import CommandRunner
from mobyai.execution.workspace import Workspace
from mobyai.execution.diagnostics import diagnose
from mobyai.engine.orchestration import advance_project
from mobyai.models.project import ProjectState
from tests.unit.test_orchestrator import _ready_project, _worker, _orchestrator, _retry_payload, _empty_payload

@pytest.mark.parametrize('message', ['Executable not found: jest. Install or configure the required toolchain.', 'sh: jest: command not found', '/bin/sh: 1: vitest: not found'])
def test_missing_node_tool_is_locally_repairable_without_sampling(message):
    diagnosis = diagnose(message)
    assert diagnosis.category == 'npm_missing_executable'
    assert diagnosis.locally_repairable
    assert not diagnosis.sampling_eligible

@pytest.mark.parametrize('message', ['Executable not found: node.', 'Executable not found: jest-other.', 'EBADENGINE', 'permission denied', 'context budget exhausted'])
def test_system_toolchain_and_resource_failures_still_pause(message):
    diagnosis = diagnose(message)
    assert not diagnosis.locally_repairable
    assert not diagnosis.sampling_eligible

async def test_local_executable_found_from_nested_cwd_and_arguments_preserved(tmp_path):
    root = tmp_path / 'project'
    binary = root / 'node_modules/.bin/moby-test-tool'
    binary.parent.mkdir(parents=True)
    (root / 'package.json').write_text('{}')
    (root / 'tests').mkdir()
    binary.write_text(f'#!{sys.executable}\nimport sys\nprint(sys.argv[1])\n')
    binary.chmod(0o755)
    command = ['moby-test-tool','test with spaces']
    result = await CommandRunner(Workspace(root)).run(command,cwd='tests')
    assert result.exit_code == 0
    assert result.stdout.strip() == 'test with spaces'
    assert result.command == command
    # Parent project executables cannot leak into a separate workspace.
    result = await CommandRunner(Workspace(root / 'tests')).run(command)
    assert result.exit_code == 127

async def test_missing_jest_routes_to_worker_repair_instead_of_pause(tmp_path,monkeypatch):
    store, workspace = await _ready_project(tmp_path)
    (workspace.root / 'package.json').write_text('{"devDependencies":{"jest":"^29.7.0"}}')
    task = await store.tasks.get('proj-1','STEP_001')
    await store.tasks.save('proj-1',task.model_copy(update={'test_commands':['jest tests/theme/test_theme_engine.js']}))
    # Keep the regression independent of globally installed Jest.
    monkeypatch.setenv('PATH','')
    _,worker = _worker([json.dumps(_empty_payload())])
    _,orchestrator = _orchestrator([json.dumps(_retry_payload())])
    result = await advance_project(await store.projects.get('proj-1'),store,worker,orchestrator,workspace)
    assert result.project.state == ProjectState.RETRYING
    events = await store.events.list_for_project('proj-1')
    diagnosis = next(e for e in events if e.event == 'failure_diagnosed')
    assert diagnosis.payload['category'] == 'npm_missing_executable'

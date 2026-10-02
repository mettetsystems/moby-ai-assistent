import json
import shlex
import sys
from unittest.mock import AsyncMock

import pytest

from mobyai.agents.planner import PlanValidationError, validate_dag_patch, validate_planner_output
from mobyai.engine.working import _run_harness_tests
from mobyai.execution.argv import CommandSyntaxError, split_command
from mobyai.execution.commands import CommandRunner
from mobyai.execution.diagnostics import diagnose
from mobyai.execution.logs import AttemptLogStore
from mobyai.execution.tests import TestRunner
from mobyai.execution.tools import ToolDispatcher
from mobyai.execution.workspace import Workspace
from mobyai.models.planner import DAGPatch, PlannerOutput
from mobyai.models.worker import WorkerToolCall
from tests.helpers import make_task
from tests.unit.test_planner import VALID_PLAN, _agent
from tests.unit.test_replan import _patch_payload


@pytest.mark.parametrize('command', [
    'source .venv/bin/activate', '. .venv/bin/activate', 'cd backend',
    'export PATH=.venv/bin', 'PYTHONPATH=src pytest',
    'pytest && echo ok', 'pytest&&echo ok', 'pytest | tee results',
    'pytest>results', 'pytest 2>>errors', 'pytest\necho ok',
    "python -c 'unclosed", '',
    ['.venv/bin/python', '-m', 'pip', 'list', '|', 'grep', 'torch'],
    ['pytest', '2>errors'], ['pytest', '&&', 'echo', 'ok'],
])
def test_invalid_commands_rejected(command):
    with pytest.raises(CommandSyntaxError, match='Invalid command syntax:'):
        split_command(command)


@pytest.mark.parametrize('command,expected', [
    ('.venv/bin/python -m pip show torch fastapi peft',
     ['.venv/bin/python', '-m', 'pip', 'show', 'torch', 'fastapi', 'peft']),
    ("python -c 'print(1 > 0); print(\"a|b\")'",
     ['python', '-c', 'print(1 > 0); print("a|b")']),
    ("grep -E 'a|b' input.txt", ['grep', '-E', 'a|b', 'input.txt']),
    ('python "path with spaces/check.py"', ['python', 'path with spaces/check.py']),
    ("bash -c 'echo ok && echo done'", ['bash', '-c', 'echo ok && echo done']),
])
def test_direct_commands_preserve_quoted_arguments(command, expected):
    assert split_command(command) == expected


@pytest.mark.parametrize('name', ['run_command', 'run_test'])
async def test_worker_tools_share_syntax_guard(tmp_path, name):
    dispatcher = ToolDispatcher.from_workspace(Workspace(tmp_path))
    result = await dispatcher.execute(WorkerToolCall(name=name, arguments={
        'command': ['source', '.venv/bin/activate']}))
    assert not result.ok
    assert diagnose(result.error).category == 'command_syntax'


async def test_runner_rejects_before_starting_process(tmp_path, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr('mobyai.execution.commands.asyncio.create_subprocess_exec', spawn)
    result = await CommandRunner(Workspace(tmp_path)).run(['source', '.venv/bin/activate'])
    assert result.exit_code == 2
    assert not result.timed_out
    spawn.assert_not_called()


async def test_saved_invalid_validation_is_logged_before_any_command_runs(tmp_path):
    workspace = Workspace(tmp_path)
    runner = CommandRunner(workspace)
    tests = TestRunner(runner, workspace)
    marker = "from pathlib import Path; Path('must-not-exist').touch()"
    task = make_task('STEP_001', test_commands=[
        shlex.join([sys.executable, '-c', marker]),
        'source .venv/bin/activate && pip list | grep torch',
    ])
    results, log_path = await _run_harness_tests(task, tests, AttemptLogStore(workspace), timeout_seconds=5)
    assert results[0].exit_code == 2
    assert not (tmp_path/'must-not-exist').exists()
    assert 'Invalid command syntax:' in (tmp_path/log_path).read_text()


@pytest.mark.parametrize('message', [
    'Executable not found: source. Install or configure the required toolchain.',
    'Executable not found: cd. Install or configure the required toolchain.',
    'Invalid command syntax: unsupported shell operator',
])
def test_syntax_failure_is_repairable_without_sampling(message):
    diagnosis = diagnose(message)
    assert diagnosis.category == 'command_syntax'
    assert diagnosis.locally_repairable
    assert not diagnosis.sampling_eligible


def test_plan_and_replan_reject_shell_validation():
    payload = json.loads(json.dumps(VALID_PLAN))
    payload['tasks'][0]['test_commands'] = ['source .venv/bin/activate && pytest']
    with pytest.raises(PlanValidationError, match='STEP_001 test_commands'):
        validate_planner_output(PlannerOutput.model_validate(payload))
    patch = _patch_payload()
    patch['supersede'][0]['replacements'][0]['test_commands'] = ['pytest | tee results']
    with pytest.raises(PlanValidationError, match='STEP_001B test_commands'):
        validate_dag_patch(DAGPatch.model_validate(patch), [], failed_task_id='STEP_001')


async def test_planner_repairs_command_before_accepting_plan():
    invalid = json.loads(json.dumps(VALID_PLAN))
    invalid['tasks'][0]['test_commands'] = ['source .venv/bin/activate && pytest']
    provider, planner = _agent([json.dumps(invalid), json.dumps(VALID_PLAN)])
    plan = await planner.create_plan('Implement', project_name='example_app')
    assert plan.tasks[0].test_commands == VALID_PLAN['tasks'][0]['test_commands']
    assert 'Invalid command syntax:' in provider.requests[1].messages[-1].content

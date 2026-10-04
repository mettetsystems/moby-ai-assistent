import asyncio
import json
import os
from pathlib import Path
import shlex
import socket
import sys
from unittest.mock import AsyncMock

import pytest

from mobyai.agents.planner import PlanValidationError, validate_dag_patch, validate_planner_output
from mobyai.engine.timeouts import TimeoutPolicy, operation_key
from mobyai.execution.argv import ServiceCommandError, split_command
from mobyai.execution.commands import CommandRunner, CommandResult
from mobyai.execution.diagnostics import diagnose
from mobyai.execution.service_check import COMMAND, container_command
from mobyai.execution.tests import TestRunner
from mobyai.execution.tools import ToolDispatcher
from mobyai.execution.workspace import Workspace
from mobyai.models.planner import DAGPatch, PlannerOutput
from mobyai.models.worker import WorkerToolCall
from mobyai.sandbox.backend import container_args
from mobyai.sandbox.policy import SandboxConfig, activate
from tests.unit.test_planner import VALID_PLAN
from tests.unit.test_replan import _patch_payload

SERVER = """
import os, sys, time, threading, subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
port, mode = int(sys.argv[1]), sys.argv[2]
Path('server.pid').write_text(str(os.getpid()))
if mode == 'exit':
    raise SystemExit(7)
if mode == 'hang':
    time.sleep(30)
if mode == 'descendant':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    Path('child.pid').write_text(str(child.pid))
if mode == 'noisy':
    print('HEADSTART', flush=True)
    print('x' * 200000, flush=True)
    print('TAILEND', flush=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 503 if mode == 'unhealthy' or self.path != '/healthz' else 200
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'ok')
        if mode == 'crash':
            threading.Timer(.05, lambda: os._exit(8)).start()
        if mode == 'degrade':
            globals()['mode'] = 'unhealthy'
    def log_message(self, *args):
        pass
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
"""


@pytest.fixture
def service(tmp_path):
    (tmp_path / 'server.py').write_text(SERVER)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    def command(mode='healthy', *, startup=2, observe=.3):
        return [COMMAND, '--url', f'http://127.0.0.1:{port}/healthz',
                '--startup-seconds', str(startup), '--observe-seconds', str(observe),
                '--', sys.executable, 'server.py', str(port), mode]
    return command, port


def assert_stopped(tmp_path, port):
    for filename in ('server.pid', 'child.pid'):
        path = tmp_path / filename
        if not path.exists():
            continue
        pid = int(path.read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        # Orphan grandchildren may briefly be zombies until the OS reaps them.
        assert Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].startswith('Z')
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', port))


@pytest.mark.parametrize('command', [
    '.venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000',
    '/usr/bin/python3.14 -u -m http.server 8000',
    'uvicorn backend.app:app', 'gunicorn backend.app:app',
    'hypercorn backend.app:app', 'flask --app app run',
    'python manage.py runserver',
])
def test_foreground_services_rejected_with_finite_repair(command):
    with pytest.raises(ServiceCommandError, match='Invalid service lifecycle:') as error:
        split_command(command)
    diagnosis = diagnose(str(error.value))
    assert diagnosis.category == 'service_lifecycle'
    assert diagnosis.locally_repairable and not diagnosis.sampling_eligible


@pytest.mark.parametrize('command', [
    'uvicorn --help', 'python -m uvicorn --version',
    'gunicorn --check-config app:app', 'gunicorn --print-config app:app',
    'python -m pytest', "python -c 'print(\"uvicorn\")'", 'python test_server.py',
])
def test_finite_commands_are_not_guessed_to_be_servers(command):
    assert split_command(command) == shlex.split(command)


@pytest.mark.parametrize('arguments', [
    ['--url', 'http://example.com:8000/healthz', '--', 'server'],
    ['--url', 'http://127.0.0.1/healthz', '--', 'server'],
    ['--url', 'http://127.0.0.1:8000/healthz', '--observe-seconds', 'nan', '--', 'server'],
    ['--url', 'http://127.0.0.1:8000/healthz', '--startup-seconds', 'inf', '--', 'server'],
    ['--url', 'http://127.0.0.1:8000/healthz', '--expected-status', '500', '--', 'server'],
    ['--url', 'http://127.0.0.1:8000/healthz', '--observe-seconds', '0', '--', 'server'],
    ['--url', 'http://127.0.0.1:8000/healthz', '--'],
    ['--url', 'http://127.0.0.1:8000/healthz', 'server'],
])
def test_invalid_service_check_rejected_before_execution(arguments):
    with pytest.raises(ServiceCommandError):
        split_command([COMMAND, *arguments])


@pytest.mark.parametrize('tool', ['run_command', 'run_test'])
async def test_tools_refuse_foreground_server_without_spawning(tmp_path, monkeypatch, tool):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    result = await ToolDispatcher.from_workspace(Workspace(tmp_path)).execute(
        WorkerToolCall(name=tool, arguments={'command': ['uvicorn', 'app:app']}))
    assert not result.ok
    assert 'Invalid service lifecycle:' in result.error
    spawn.assert_not_called()


async def test_guard_precedes_exhausted_timeout_budget(tmp_path, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    command = ['.venv/bin/python', '-m', 'uvicorn', 'backend.app:app']
    policy = TimeoutPolicy(None, 'demo')
    policy.exhausted[operation_key('command', [str(tmp_path), command])] = 6000
    policy.ceiling = 6000
    with policy.activate():
        result = await CommandRunner(Workspace(tmp_path)).run(command)
    assert result.exit_code == 2 and not result.timed_out
    assert diagnose(result.stderr).category == 'service_lifecycle'
    spawn.assert_not_called()


def test_initial_plan_and_replan_reject_foreground_server():
    payload = json.loads(json.dumps(VALID_PLAN))
    payload['tasks'][0]['test_commands'] = ['python -m uvicorn app:app']
    with pytest.raises(PlanValidationError, match='Invalid service lifecycle:'):
        validate_planner_output(PlannerOutput.model_validate(payload))
    patch = _patch_payload()
    patch['supersede'][0]['replacements'][0]['test_commands'] = ['uvicorn app:app']
    with pytest.raises(PlanValidationError, match='Invalid service lifecycle:'):
        validate_dag_patch(DAGPatch.model_validate(patch), [], failed_task_id='STEP_001')


async def test_successful_check_observes_then_cleans_up(tmp_path, service):
    command, port = service
    workspace = Workspace(tmp_path)
    result = await TestRunner(CommandRunner(workspace), workspace).run(command('descendant'))
    assert result.exit_code == 0 and not result.timed_out
    assert result.duration_seconds >= .3
    assert 'remained healthy for 0.3 seconds' in result.stderr
    assert_stopped(tmp_path, port)


@pytest.mark.parametrize('mode,reason', [
    ('exit', 'exited early with code 7'), ('hang', 'startup deadline exceeded'),
    ('unhealthy', 'HTTP 503'), ('crash', 'exited early'),
    ('degrade', 'health check failed during observation'),
])
async def test_failed_service_never_passes_and_is_cleaned_up(tmp_path, service, mode, reason):
    command, port = service
    result = await CommandRunner(Workspace(tmp_path)).run(command(mode, startup=.5))
    assert result.exit_code == 1 and not result.timed_out
    assert reason in result.stderr
    assert_stopped(tmp_path, port)


async def test_cancellation_cleans_up_server_and_descendants(tmp_path, service):
    command, port = service
    task = asyncio.create_task(CommandRunner(Workspace(tmp_path)).run(command('descendant', observe=30)))
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / 'child.pid').exists():
                await asyncio.sleep(.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert_stopped(tmp_path, port)


async def test_outer_operation_timeout_still_applies(tmp_path, service):
    command, port = service
    result = await CommandRunner(Workspace(tmp_path)).run(command(observe=30), timeout_seconds=1)
    assert result.timed_out and result.exit_code == -1
    assert_stopped(tmp_path, port)


async def test_occupied_port_does_not_validate_an_unrelated_service(tmp_path, service, monkeypatch):
    command, port = service
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', port))
        occupied.listen()
        result = await CommandRunner(Workspace(tmp_path)).run(command())
    assert result.exit_code == 1
    spawn.assert_not_called()


async def test_service_output_remains_bounded(tmp_path, service):
    command, port = service
    result = await CommandRunner(Workspace(tmp_path), max_output_bytes=1024).run(command('noisy'))
    assert result.exit_code == 0
    assert result.stdout_truncated
    assert 'HEADSTART' in result.stdout and 'TAILEND' in result.stdout
    assert len(result.stdout) < 1100
    assert_stopped(tmp_path, port)


async def test_service_command_preserves_execution_policy(tmp_path):
    command = [COMMAND, '--url', 'http://127.0.0.1:8000/', '--', 'sudo', 'server']
    space = Workspace(tmp_path)
    result = await TestRunner(CommandRunner(space), space).run(command)
    assert result.exit_code == 126


async def test_sandbox_check_does_not_probe_or_spawn_on_host(tmp_path, monkeypatch):
    command = [COMMAND, '--url', 'http://127.0.0.1:8000/', '--', 'uvicorn', 'app:app']
    remote = AsyncMock(return_value=CommandResult(command=command, exit_code=125,
        stdout='', stderr='Sandbox unavailable: missing image', duration_seconds=0))
    host = AsyncMock(side_effect=AssertionError('host probe attempted'))
    monkeypatch.setattr('mobyai.sandbox.client.execute', remote)
    monkeypatch.setattr('mobyai.execution.service_check.run_check', host)
    with activate(SandboxConfig(enabled=True, image='sha256:'+'a'*64)):
        result = await CommandRunner(Workspace(tmp_path)).run(command)
    assert result.exit_code == 125
    assert remote.call_args.args[2] == command
    host.assert_not_called()


def test_sandbox_places_probe_inside_the_same_offline_container(tmp_path):
    command = [COMMAND, '--url', 'http://127.0.0.1:8000/', '--', 'uvicorn', 'app:app']
    root = tmp_path / 'project'; root.mkdir()
    deps = tmp_path / 'deps'; deps.mkdir()
    args = container_args(SandboxConfig(enabled=True, image='sha256:'+'a'*64),
                          Workspace(root), root, deps, 'test', command)
    assert '--network=none' in args
    assert args[args.index('--entrypoint')+1] == 'python3'
    assert args[-len(command[1:]):] == command[1:]
    assert 'async def run_check' in args[args.index('-c')+1]


async def test_standalone_probe_source_needs_no_moby_installation(tmp_path, service):
    command, port = service
    expanded = container_command(command())
    process = await asyncio.create_subprocess_exec(sys.executable, *expanded[1:], cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, stderr.decode()
    assert b'Service check passed' in stderr
    assert_stopped(tmp_path, port)

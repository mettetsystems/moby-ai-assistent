import sys
from pathlib import Path

import pytest

from mobyai.execution.commands import CommandRunner
from mobyai.execution.errors import DestructiveCommandError
from mobyai.execution.logs import AttemptLogStore
from mobyai.execution.tests import TestRunner, passed
from mobyai.execution.workspace import Workspace

PYTHON = sys.executable


def _runner(
    tmp_path: Path, *, max_output_bytes: int = 1_048_576
) -> tuple[Workspace, CommandRunner]:
    root = tmp_path / "project"
    root.mkdir()
    space = Workspace(root)
    runner = CommandRunner(space, max_output_bytes=max_output_bytes)
    return space, runner


async def test_successful_command(tmp_path: Path) -> None:
    _space, runner = _runner(tmp_path)
    result = await runner.run([PYTHON, "-c", "print('ok')"], timeout_seconds=10)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "ok" in result.stdout


async def test_failed_command_captures_stderr(tmp_path: Path) -> None:
    _space, runner = _runner(tmp_path)
    result = await runner.run(
        [PYTHON, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
        timeout_seconds=10,
    )
    assert result.exit_code == 3
    assert "boom" in result.stderr


async def test_timeout(tmp_path: Path) -> None:
    _space, runner = _runner(tmp_path)
    result = await runner.run(
        [PYTHON, "-c", "import time; time.sleep(30)"],
        timeout_seconds=1,
    )
    assert result.timed_out is True
    assert result.exit_code == -1


async def test_large_output_keeps_head_and_tail(tmp_path: Path) -> None:
    _space, runner = _runner(tmp_path, max_output_bytes=2048)
    result = await runner.run(
        [
            PYTHON,
            "-c",
            "print('HEADSTART'); print('x' * 200000); print('TAILEND')",
        ],
        timeout_seconds=10,
    )
    assert result.stdout_truncated is True
    assert "HEADSTART" in result.stdout
    assert "TAILEND" in result.stdout
    assert len(result.stdout.encode("utf-8")) < 200000


async def test_destructive_rm_is_denied(tmp_path: Path) -> None:
    _space, runner = _runner(tmp_path)
    with pytest.raises(DestructiveCommandError):
        await runner.run(["rm", "-rf", "src"], timeout_seconds=5)


async def test_test_runner_pass_and_fail(tmp_path: Path) -> None:
    space, runner = _runner(tmp_path)
    tests = TestRunner(runner, space)
    ok = await tests.run([PYTHON, "-c", "raise SystemExit(0)"])
    assert passed(ok)
    bad = await tests.run([PYTHON, "-c", "raise SystemExit(1)"])
    assert not passed(bad)


async def test_attempt_log_is_written_inside_workspace(tmp_path: Path) -> None:
    space, runner = _runner(tmp_path)
    result = await runner.run([PYTHON, "-c", "print('logged')"], timeout_seconds=10)
    store = AttemptLogStore(space)
    path = await store.persist("STEP_001", 1, result)
    assert path == space.root / "artifacts" / "logs" / "STEP_001" / "attempt_001.log"
    text = path.read_text(encoding="utf-8")
    assert "exit_code: 0" in text
    assert "logged" in text

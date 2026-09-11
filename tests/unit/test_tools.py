import sys
from pathlib import Path

import pytest

from mobyai.execution.commands import CommandRunner
from mobyai.execution.errors import WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.tests import TestRunner
from mobyai.execution.tools import ToolDispatcher, split_command
from mobyai.execution.workspace import Workspace
from mobyai.models.worker import WorkerToolCall, WorkerToolName

PYTHON = sys.executable


def _dispatcher(tmp_path: Path) -> tuple[Workspace, WorkspaceFilesystem, ToolDispatcher]:
    root = tmp_path / "project"
    root.mkdir()
    space = Workspace(root)
    files = WorkspaceFilesystem(space)
    commands = CommandRunner(space)
    tests = TestRunner(commands, space)
    return space, files, ToolDispatcher(files, space, commands, tests, timeout_seconds=10)


def test_split_command_parses_string_and_argv() -> None:
    assert split_command(f"{PYTHON} -c 'print(1)'")[0] == PYTHON
    assert split_command(["pytest", "tests/test_app.py"]) == ["pytest", "tests/test_app.py"]
    with pytest.raises(ValueError):
        split_command("")
    with pytest.raises(ValueError):
        split_command(["pytest", ""])


async def test_write_and_read_file(tmp_path: Path) -> None:
    _space, files, dispatcher = _dispatcher(tmp_path)
    written = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.WRITE_FILE,
            arguments={"path": "src/app.py", "contents": "ok\n"},
        )
    )
    assert written.ok
    assert written.changed_files == ["src/app.py"]
    assert await files.read_file("src/app.py") == "ok\n"
    read = await dispatcher.execute(
        WorkerToolCall(name=WorkerToolName.READ_FILE, arguments={"path": "src/app.py"})
    )
    assert read.ok
    assert read.output == "ok\n"


async def test_write_file_escape_is_not_applied(tmp_path: Path) -> None:
    space, _files, dispatcher = _dispatcher(tmp_path)
    outcome = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.WRITE_FILE,
            arguments={"path": "../secret.py", "contents": "nope\n"},
        )
    )
    assert outcome.ok is False
    assert "escape" in (outcome.error or "").lower()
    assert not (tmp_path / "secret.py").exists()
    assert not (space.root / "secret.py").exists()


async def test_apply_patch_and_list_and_search(tmp_path: Path) -> None:
    _space, files, dispatcher = _dispatcher(tmp_path)
    await files.write_file("src/hello.py", "hello\n")
    patched = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.APPLY_PATCH,
            arguments={
                "diff": (
                    "--- a/src/hello.py\n"
                    "+++ b/src/hello.py\n"
                    "@@ -1 +1 @@\n"
                    "-hello\n"
                    "+hello world\n"
                )
            },
        )
    )
    assert patched.ok
    assert patched.changed_files == ["src/hello.py"]
    listed = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.LIST_DIRECTORY,
            arguments={"path": "src"},
        )
    )
    assert "src/hello.py" in listed.output
    found = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.SEARCH_CODE,
            arguments={"pattern": "hello world", "path": "src"},
        )
    )
    assert "src/hello.py" in found.output


async def test_run_command_and_run_test(tmp_path: Path) -> None:
    _space, _files, dispatcher = _dispatcher(tmp_path)
    command = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.RUN_COMMAND,
            arguments={"command": [PYTHON, "-c", "print('hi')"]},
        )
    )
    assert command.ok
    assert "hi" in command.output
    test = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.RUN_TEST,
            arguments={"command": f"{PYTHON} -c \"raise SystemExit(0)\""},
        )
    )
    assert test.ok
    assert test.exit_code == 0


async def test_destructive_command_is_denied(tmp_path: Path) -> None:
    _space, _files, dispatcher = _dispatcher(tmp_path)
    outcome = await dispatcher.execute(
        WorkerToolCall(
            name=WorkerToolName.RUN_COMMAND,
            arguments={"command": ["rm", "-rf", "."]},
        )
    )
    assert outcome.ok is False
    assert "Destructive" in (outcome.error or "")


async def test_missing_arguments_fail_without_writes(tmp_path: Path) -> None:
    _space, files, dispatcher = _dispatcher(tmp_path)
    outcome = await dispatcher.execute(
        WorkerToolCall(name=WorkerToolName.WRITE_FILE, arguments={"path": "src/app.py"})
    )
    assert outcome.ok is False
    with pytest.raises(WorkspaceError):
        await files.read_file("src/app.py")


async def test_read_file_range_has_provenance(tmp_path: Path) -> None:
    _, files, dispatcher = _dispatcher(tmp_path)
    await files.write_file("sample.txt", "one\ntwo\nthree\nfour\n")
    result = await dispatcher.execute(WorkerToolCall(name="read_file",
        arguments={"path": "sample.txt", "start_line": 2, "max_lines": 2}))
    assert result.ok and result.output == "two\nthree\n"
    assert result.source_path == "sample.txt"
    assert (result.start_line, result.end_line, result.total_lines) == (2, 3, 4)
    assert result.truncated


@pytest.mark.parametrize("arguments", [{"start_line": 0}, {"start_line": -1}, {"max_lines": 0}, {"max_lines": 2001}, {"max_lines": True}])
async def test_read_file_rejects_invalid_ranges(tmp_path: Path, arguments: dict) -> None:
    _, files, dispatcher = _dispatcher(tmp_path)
    await files.write_file("sample.txt", "hello")
    result = await dispatcher.execute(WorkerToolCall(name="read_file", arguments={"path": "sample.txt", **arguments}))
    assert not result.ok

from pathlib import Path

import pytest

from mobyai.context.errors import ContextLookupError
from mobyai.context.log_reducer import LogReducer
from mobyai.context.packer import ContextPacker
from mobyai.context.tokenizer import estimate_tokens, truncate_to_tokens
from mobyai.context.types import FileRole
from mobyai.execution.commands import CommandResult
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.logs import AttemptLogStore, format_command_log, parse_command_log
from mobyai.execution.workspace import Workspace
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.task import TaskStatus
from mobyai.state.memory import InMemoryStateStore
from tests.helpers import FROZEN_NOW, make_project, make_task

UNRELATED = "UNRELATED_FILE_BODY_SHOULD_NOT_APPEAR"
LEAKED_HISTORY = "PAST_WORKER_CHAT_MUST_NOT_LEAK"
OTHER_TASK_SECRET = "OTHER_TASK_FILE_SECRET"
MIDDLE_NOISE = "MIDDLE_NOISE_LINE_SHOULD_BE_STRIPPED"


def _workspace(tmp_path: Path) -> tuple[Workspace, WorkspaceFilesystem]:
    root = tmp_path / "project"
    root.mkdir()
    space = Workspace(root)
    return space, WorkspaceFilesystem(space)


async def _write(files: WorkspaceFilesystem, path: str, content: str) -> None:
    await files.write_file(path, content)


def test_estimate_tokens_is_char_over_four() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_truncate_to_tokens_keeps_head_and_tail() -> None:
    text = "H" * 80 + "MIDDLE" + "T" * 80
    sliced, truncated = truncate_to_tokens(text, 20)
    assert truncated
    assert estimate_tokens(sliced) <= 20
    assert sliced.startswith("H")
    assert sliced.endswith("T")
    assert "MIDDLE" not in sliced
    assert "...[truncated]..." in sliced


async def test_includes_target_import_and_test_excludes_unrelated(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    await _write(
        files,
        "src/app.py",
        "from src.db import connect\nimport os\n\ndef main():\n    return connect()\n",
    )
    await _write(files, "src/db.py", "def connect():\n    return 1\n")
    await _write(files, "src/contracts.py", "class AppAPI:\n    pass\n")
    await _write(files, "src/secrets.py", f"SECRET = {UNRELATED!r}\n")
    await _write(files, "tests/test_app.py", "def test_main():\n    assert True\n")

    packer = ContextPacker(files)
    project = make_project(workspace_root=space.root)
    task = make_task(
        "STEP_001",
        name="Wire the app to the database",
        description="Call connect() from main.",
        target_files=["src/app.py"],
        test_files=["tests/test_app.py"],
        required_context=["src/contracts.py"],
        acceptance_criteria=["main returns a connection token"],
        test_commands=["pytest tests/test_app.py"],
        status=TaskStatus.READY,
    )
    packed = await packer.pack(project, task, token_budget=48000)
    rendered = packed.render()

    assert packed.paths_for(FileRole.TARGET) == ["src/app.py"]
    assert packed.paths_for(FileRole.TEST) == ["tests/test_app.py"]
    assert packed.paths_for(FileRole.IMPORT) == ["src/db.py"]
    assert packed.paths_for(FileRole.REQUIRED_CONTEXT) == ["src/contracts.py"]
    assert "src/secrets.py" not in packed.paths_for(FileRole.IMPORT)
    assert UNRELATED not in rendered
    assert "def connect()" in rendered
    assert "class AppAPI" in rendered
    assert "main returns a connection token" in rendered
    assert "pytest tests/test_app.py" in rendered
    assert "import os" in rendered
    assert packed.token_count <= packed.token_budget
    assert packed.token_count == estimate_tokens(rendered)


async def test_token_budget_omits_low_priority_files(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    await _write(files, "src/app.py", "from src.heavy import blob\n\ndef run():\n    return blob\n")
    await _write(files, "src/heavy.py", "blob = '" + ("X" * 8000) + "'\n")

    packer = ContextPacker(files, constraints=("Stay in the workspace.",))
    packed = await packer.pack(
        make_project(workspace_root=space.root),
        make_task(
            "STEP_001",
            description="Use the helper.",
            target_files=["src/app.py"],
            acceptance_criteria=["run() works"],
            test_commands=["pytest"],
        ),
        token_budget=250,
    )
    assert packed.paths_for(FileRole.TARGET) == ["src/app.py"]
    assert packed.paths_for(FileRole.IMPORT) == []
    assert "src/heavy.py" in packed.omitted_paths
    assert packed.token_count <= packed.token_budget
    assert "X" * 100 not in packed.render()


async def test_oversize_target_is_truncated_to_budget(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    await _write(files, "src/app.py", "HEADMARKER\n" + ("Y" * 20000) + "\nTAILMARKER\n")
    packer = ContextPacker(files, constraints=("Stay in the workspace.",))
    packed = await packer.pack(
        make_project(workspace_root=space.root),
        make_task("STEP_001", target_files=["src/app.py"]),
        token_budget=400,
    )
    assert packed.files[0].truncated
    rendered = packed.render()
    assert "HEADMARKER" in rendered
    assert "TAILMARKER" in rendered
    assert packed.token_count <= packed.token_budget


async def test_retry_uses_reduced_log_not_full_file(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    await _write(files, "src/app.py", "def run():\n    return 1\n")
    stdout_lines = [f"line-{index}" for index in range(1, 401)]
    stdout_lines[199] = MIDDLE_NOISE
    result = CommandResult(
        command=["pytest", "tests/test_app.py"],
        exit_code=1,
        stdout="\n".join(stdout_lines) + "\n",
        stderr=(
            "Traceback (most recent call last):\n"
            '  File "tests/test_app.py", line 4, in test_run\n'
            "    assert got == want\n"
            "AssertionError\n"
            "E       AssertionError: assert 1 == 2\n"
            "FAILED tests/test_app.py::test_run\n"
        ),
        duration_seconds=1.2,
    )
    logs = AttemptLogStore(space)
    stored = await logs.persist("STEP_001", 1, result)
    relative_log = space.relative(stored)

    store = InMemoryStateStore()
    project = make_project(workspace_root=space.root)
    task = make_task(
        "STEP_001",
        target_files=["src/app.py"],
        attempt_count=1,
        acceptance_criteria=["run returns 2"],
        test_commands=["pytest tests/test_app.py"],
    )
    await store.projects.save(project)
    await store.tasks.save(project.project_id, task)
    await store.attempts.save(
        TaskAttempt(
            project_id=project.project_id,
            task_id="STEP_001",
            attempt_number=1,
            started_at=FROZEN_NOW,
            finished_at=FROZEN_NOW,
            worker_response={"summary": LEAKED_HISTORY},
            test_exit_code=1,
            test_log_path=relative_log,
            result=AttemptResult.FAIL,
        )
    )

    packer = ContextPacker(files, store, log_reducer=LogReducer(tail_lines=5))
    packed = await packer.build_worker_context(project.project_id, "STEP_001", 8000)
    rendered = packed.render()
    assert packed.reduced_error is not None
    assert packed.reduced_error.failed_tests == ["tests/test_app.py::test_run"]
    assert packed.reduced_error.traceback is not None
    assert MIDDLE_NOISE not in rendered
    assert "line-150" not in rendered
    assert "tests/test_app.py::test_run" in rendered
    assert LEAKED_HISTORY not in rendered
    assert relative_log not in packed.paths_for(FileRole.TARGET)
    full_log = format_command_log(result)
    assert len(rendered) < len(full_log)


async def test_excludes_other_task_files_and_history(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    await _write(files, "src/app.py", "def run():\n    return 1\n")
    await _write(files, "src/other.py", f"SECRET = {OTHER_TASK_SECRET!r}\n")

    store = InMemoryStateStore()
    project = make_project(
        workspace_root=space.root,
        architecture_summary="Small example app.",
    )
    current = make_task(
        "STEP_002",
        target_files=["src/app.py"],
        required_context=[],
        acceptance_criteria=["run works"],
        test_commands=["pytest"],
    )
    other = make_task("STEP_001", target_files=["src/other.py"])
    await store.projects.save(project)
    await store.tasks.save_all(project.project_id, [other, current])
    await store.attempts.save(
        TaskAttempt(
            project_id=project.project_id,
            task_id="STEP_001",
            attempt_number=1,
            started_at=FROZEN_NOW,
            worker_response={"summary": LEAKED_HISTORY, "notes": "full chat dump"},
            result=AttemptResult.PASS,
        )
    )

    packer = ContextPacker(files, store)
    packed = await packer.build_worker_context(project.project_id, "STEP_002", 48000)
    rendered = packed.render()
    assert OTHER_TASK_SECRET not in rendered
    assert LEAKED_HISTORY not in rendered
    assert packed.reduced_error is None
    assert packed.paths_for(FileRole.TARGET) == ["src/app.py"]
    assert "src/other.py" not in [item.path for item in packed.files]


async def test_missing_target_is_marked(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    packer = ContextPacker(files)
    packed = await packer.pack(
        make_project(workspace_root=space.root),
        make_task("STEP_001", target_files=["src/missing.py"]),
        token_budget=2000,
    )
    assert packed.files[0].path == "src/missing.py"
    assert packed.files[0].missing
    assert "missing" in packed.render()


async def test_unknown_project_and_task_raise(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    store = InMemoryStateStore()
    await store.projects.save(make_project(workspace_root=space.root))
    packer = ContextPacker(files, store)
    with pytest.raises(ContextLookupError):
        await packer.build_worker_context("nope", "STEP_001", 1000)
    with pytest.raises(ContextLookupError):
        await packer.build_worker_context("proj-1", "STEP_999", 1000)


def test_parse_command_log_round_trip() -> None:
    result = CommandResult(
        command=["pytest", "tests/test_app.py"],
        exit_code=1,
        stdout="ok\n",
        stderr="boom\n",
        duration_seconds=0.5,
        timed_out=False,
    )
    parsed = parse_command_log(format_command_log(result))
    assert parsed.command == result.command
    assert parsed.exit_code == 1
    assert parsed.stdout == "ok\n"
    assert parsed.stderr == "boom\n"

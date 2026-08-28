from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path

import pytest

from mobyai.cli import async_main, parse_args
from mobyai.models.config import AppConfig, ServiceMode
from mobyai.setup import RoleAnswers, SetupAnswers, SetupError, render_config_yaml, run_setup

REPO_ROOT = Path(__file__).resolve().parents[2]


class _TtyIn(StringIO):
    def isatty(self) -> bool:
        return True


async def _setup(argv: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    code = await async_main(
        argv,
        stdout=stdout,
        stderr=stderr,
        stdin=_TtyIn(stdin) if stdin else StringIO(),
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_parse_args_setup() -> None:
    args = parse_args(["setup", "--non-interactive", "--mode", "managed", "-o", "out.yaml"])
    assert args.command == "setup"
    assert args.non_interactive is True
    assert args.mode == "managed"
    assert args.output == "out.yaml"


async def test_setup_non_interactive_writes_external_config(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    workspace = tmp_path / "projects"
    database = tmp_path / "state" / "mobyai.db"
    code, out, err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--output",
            str(dest),
            "--workspace",
            str(workspace),
            "--database",
            str(database),
            "--planner-model",
            "plan-id",
            "--orchestrator-model",
            "orch-id",
            "--worker-model",
            "work-id",
        ]
    )
    assert err == ""
    assert code == 0
    assert dest.is_file()
    config = AppConfig.from_yaml(dest)
    assert config.services.mode is ServiceMode.EXTERNAL
    assert config.models.planner.model == "plan-id"
    assert config.models.planner.launch == []
    assert config.project.workspace_root == workspace
    assert config.persistence.database_path == database
    assert workspace.is_dir()
    assert database.parent.is_dir()
    assert "Next steps" in out
    assert "WARNING" not in out


async def test_setup_managed_includes_launch_argv(tmp_path: Path) -> None:
    dest = tmp_path / "managed.yaml"
    code, out, err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--output",
            str(dest),
            "--mode",
            "managed",
            "--workspace",
            str(tmp_path / "projects"),
            "--database",
            str(tmp_path / "mobyai.db"),
            "--no-directories",
        ]
    )
    assert err == ""
    assert code == 0
    config = AppConfig.from_yaml(dest)
    assert config.services.mode is ServiceMode.MANAGED
    assert config.models.worker.launch[0] == "vllm"
    assert "{model}" in config.models.planner.launch
    assert "{port}" in config.models.planner.launch
    assert "WARNING" in out


async def test_setup_warns_on_placeholder_models(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    code, out, _err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--output",
            str(dest),
            "--workspace",
            str(tmp_path / "p"),
            "--database",
            str(tmp_path / "db.sqlite"),
            "--no-directories",
        ]
    )
    assert code == 0
    assert "WARNING" in out
    assert "placeholder model ids" in out


async def test_setup_refuses_overwrite_without_force(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    dest.write_text("already\n", encoding="utf-8")
    code, _out, err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--output",
            str(dest),
            "--no-directories",
        ]
    )
    assert code == 1
    assert "already exists" in err
    assert dest.read_text(encoding="utf-8") == "already\n"


async def test_setup_force_overwrites(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    dest.write_text("already\n", encoding="utf-8")
    code, _out, err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--force",
            "--output",
            str(dest),
            "--workspace",
            str(tmp_path / "p"),
            "--database",
            str(tmp_path / "db.sqlite"),
            "--no-directories",
        ]
    )
    assert err == ""
    assert code == 0
    AppConfig.from_yaml(dest)


async def test_setup_interactive_prompts(tmp_path: Path) -> None:
    dest = tmp_path / "asked.yaml"
    answers = "\n".join(
        [
            str(tmp_path / "ws"),
            str(tmp_path / "db.sqlite"),
            "external",
            "p-model",
            "http://127.0.0.1:1802",
            "0",
            "o-model",
            "http://127.0.0.1:1801",
            "1",
            "w-model",
            "http://127.0.0.1:1800",
            "cpu",
            "n",
        ]
    )
    code, out, err = await _setup(
        ["setup", "--output", str(dest)],
        stdin=answers + "\n",
    )
    assert err == ""
    assert code == 0
    config = AppConfig.from_yaml(dest)
    assert config.models.planner.model == "p-model"
    assert config.models.worker.gpu is None
    assert config.hardware is not None
    assert config.hardware.worker.device == "cpu"
    assert "Press Enter" in out
    assert not (tmp_path / "ws").exists()


async def test_setup_rejects_non_tty_without_flag(tmp_path: Path) -> None:
    stdout = StringIO()
    stderr = StringIO()
    code = await async_main(
        ["setup", "--output", str(tmp_path / "x.yaml")],
        stdout=stdout,
        stderr=stderr,
        stdin=StringIO(""),
    )
    assert code == 1
    assert "not a terminal" in stderr.getvalue()


def test_setup_script_help() -> None:
    script = REPO_ROOT / "scripts" / "setup.sh"
    result = subprocess.run(
        ["bash", str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Walks through" in result.stdout
    assert "mobyai setup" in result.stdout


def test_render_config_round_trip() -> None:
    text = render_config_yaml(
        SetupAnswers(
            output=Path("config.yaml"),
            workspace="~/MobyAI/projects",
            database="~/MobyAI/mobyai.db",
            mode="external",
            planner=RoleAnswers("a", "http://127.0.0.1:1", "0"),
            orchestrator=RoleAnswers("b", "http://127.0.0.1:2", "1"),
            worker=RoleAnswers("c", "http://127.0.0.1:3", "0"),
            create_directories=False,
        )
    )
    config = AppConfig.from_yaml_text(text)
    assert config.models.worker.model == "c"


def test_run_setup_bad_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="external.*managed"):
        run_setup(
            output=tmp_path / "c.yaml",
            non_interactive=True,
            mode="sidecar",
            stdin=StringIO(),
            stdout=StringIO(),
            create_directories=False,
        )

from __future__ import annotations

import stat
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from mobyai.cli import async_main, parse_args
from mobyai.models.config import AppConfig, ServiceMode
from mobyai.setup import (
    DEFAULT_ROLES,
    RoleAnswers,
    SetupAnswers,
    SetupError,
    render_config_yaml,
    run_setup,
)

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


def _base_argv(tmp_path: Path, dest: Path) -> list[str]:
    return [
        "setup",
        "--non-interactive",
        "--output",
        str(dest),
        "--workspace",
        str(tmp_path / "projects"),
        "--database",
        str(tmp_path / "mobyai.db"),
        "--hf-token-file",
        str(tmp_path / "secrets" / "hf_token"),
        "--skip-hf-token",
        "--skip-download",
        "--no-directories",
    ]


def test_parse_args_setup() -> None:
    args = parse_args(
        [
            "setup",
            "--non-interactive",
            "--preset",
            "default",
            "--mode",
            "managed",
            "-o",
            "out.yaml",
            "--skip-hf-token",
        ]
    )
    assert args.command == "setup"
    assert args.non_interactive is True
    assert args.preset == "default"
    assert args.mode == "managed"
    assert args.output == "out.yaml"
    assert args.skip_hf_token is True


async def test_setup_default_preset_is_managed_qwen_stack(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    code, out, err = await _setup(_base_argv(tmp_path, dest))
    assert err == ""
    assert code == 0
    config = AppConfig.from_yaml(dest)
    assert config.services.mode is ServiceMode.MANAGED
    assert config.models.planner.model == DEFAULT_ROLES["planner"]["model"]
    assert config.models.orchestrator.model == DEFAULT_ROLES["orchestrator"]["model"]
    assert config.models.worker.model == DEFAULT_ROLES["worker"]["model"]
    assert config.models.worker.launch[0] == "llama"
    assert config.huggingface.token_file == tmp_path / "secrets" / "hf_token"
    assert "hf_test" not in dest.read_text(encoding="utf-8")
    assert "Next steps" in out


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
            "--preset",
            "external",
            "--workspace",
            str(workspace),
            "--database",
            str(database),
            "--hf-token-file",
            str(tmp_path / "hf_token"),
            "--skip-hf-token",
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
    config = AppConfig.from_yaml(dest)
    assert config.services.mode is ServiceMode.EXTERNAL
    assert config.models.planner.model == "plan-id"
    assert config.models.planner.launch == []
    assert config.project.workspace_root == workspace
    assert config.persistence.database_path == database
    assert workspace.is_dir()
    assert "WARNING" not in out or "placeholder" not in out.lower()


async def test_setup_token_goes_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    dest = tmp_path / "config.yaml"
    token_file = tmp_path / "secrets" / "hf_token"
    argv = [
        "setup",
        "--non-interactive",
        "--output",
        str(dest),
        "--workspace",
        str(tmp_path / "p"),
        "--database",
        str(tmp_path / "db.sqlite"),
        "--hf-token-file",
        str(token_file),
        "--hf-token",
        "hf_test_secret_abcd",
        "--skip-download",
        "--no-directories",
        "--preset",
        "external",
    ]
    code, out, err = await _setup(argv)
    assert err == ""
    assert code == 0
    assert token_file.read_text(encoding="utf-8").strip() == "hf_test_secret_abcd"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    yaml_text = dest.read_text(encoding="utf-8")
    assert "hf_test_secret_abcd" not in yaml_text
    assert "token_file" in yaml_text
    assert "mode 0600" in out


async def test_setup_managed_includes_launch_argv(tmp_path: Path) -> None:
    dest = tmp_path / "managed.yaml"
    code, out, err = await _setup(_base_argv(tmp_path, dest) + ["--mode", "managed"])
    assert err == ""
    assert code == 0
    config = AppConfig.from_yaml(dest)
    assert config.services.mode is ServiceMode.MANAGED
    assert "{model}" in config.models.planner.launch
    assert "{port}" in config.models.planner.launch
    assert "{ctx}" in config.models.planner.launch
    assert config.models.planner.launch[0] == "llama"
    assert "-hf" in config.models.planner.launch
    assert "--ctx-size" in config.models.planner.launch
    assert "--flash-attn" in config.models.planner.launch
    assert "--parallel" in config.models.planner.launch
    assert config.models.planner.inference is not None
    assert config.models.planner.inference.max_model_len == 32768
    assert config.models.orchestrator.inference is not None
    assert config.models.orchestrator.inference.max_model_len == 16384
    assert config.models.worker.inference is not None
    assert config.models.worker.inference.max_model_len == 65536
    assert config.models.worker.inference.gpu_memory_utilization is None
    assert "WARNING" in out
    assert "Hugging Face token" in out or "no Hugging Face token" in out


async def test_setup_warns_on_legacy_placeholder_models(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    code, out, _err = await _setup(
        _base_argv(tmp_path, dest)
        + [
            "--preset",
            "external",
            "--planner-model",
            "configurable-planner-model",
            "--orchestrator-model",
            "configurable-small-model",
            "--worker-model",
            "configurable-coding-model",
        ]
    )
    assert code == 0
    assert "WARNING" in out
    assert "placeholder" in out.lower()


async def test_setup_refuses_overwrite_without_force(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    dest.write_text("already\n", encoding="utf-8")
    code, _out, err = await _setup(_base_argv(tmp_path, dest))
    assert code == 1
    assert "already exists" in err
    assert dest.read_text(encoding="utf-8") == "already\n"


async def test_setup_force_overwrites(tmp_path: Path) -> None:
    dest = tmp_path / "config.yaml"
    dest.write_text("already\n", encoding="utf-8")
    code, _out, err = await _setup(_base_argv(tmp_path, dest) + ["--force"])
    assert err == ""
    assert code == 0
    AppConfig.from_yaml(dest)


async def test_setup_interactive_prompts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("mobyai.setup.detect_nvidia_gpus", lambda: [])
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
        [
            "setup",
            "--output",
            str(dest),
            "--hf-token-file",
            str(tmp_path / "hf_token"),
            "--skip-hf-token",
            "--skip-download",
            "--preset",
            "custom",
        ],
        stdin=answers + "\n",
    )
    assert err == ""
    assert code == 0
    config = AppConfig.from_yaml(dest)
    assert config.services.mode is ServiceMode.EXTERNAL
    assert config.models.planner.model == "p-model"
    assert config.models.worker.gpu is None
    assert config.hardware is not None
    assert config.hardware.worker.device == "cpu"
    assert "Press Enter to keep" in out
    assert not (tmp_path / "ws").exists()


async def test_setup_interactive_accepts_hf_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    dest = tmp_path / "cfg.yaml"
    token_file = tmp_path / "tok"
    answers = "\n".join(
        [
            str(tmp_path / "ws"),
            str(tmp_path / "db.sqlite"),
            "hf_interactive_zzzz",
            "default",
            "",  # planner model default
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "n",
        ]
    )
    code, out, err = await _setup(
        ["setup", "--output", str(dest), "--hf-token-file", str(token_file), "--skip-download"],
        stdin=answers + "\n",
    )
    assert err == ""
    assert code == 0
    assert token_file.read_text(encoding="utf-8").strip() == "hf_interactive_zzzz"
    assert "hf_interactive_zzzz" not in dest.read_text(encoding="utf-8")
    config = AppConfig.from_yaml(dest)
    assert config.services.mode is ServiceMode.MANAGED
    assert config.models.worker.model == DEFAULT_ROLES["worker"]["model"]
    assert "Press Enter to keep" in out


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
    assert "bootstrap" in result.stdout.lower() or "wizard" in result.stdout.lower()
    assert "--hf-token" in result.stdout
    assert "--preset" in result.stdout
    assert "--download-models" in result.stdout
    assert "--skip-download" in result.stdout
    assert "--skip-runtime" in result.stdout
    assert "--skip-endpoint-test" in result.stdout


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
            hf_token="",
            hf_token_file="~/MobyAI/secrets/hf_token",
            preset="external",
            download_models=False,
            install_runtime=False,
            endpoint_test=False,
            llama_binary=None,
        )
    )
    config = AppConfig.from_yaml_text(text)
    assert config.models.worker.model == "c"
    assert config.huggingface.token_file is not None


def test_run_setup_bad_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="external.*managed"):
        run_setup(
            output=tmp_path / "c.yaml",
            non_interactive=True,
            mode="sidecar",
            skip_hf_token=True,
            hf_token_file=str(tmp_path / "hf"),
            create_directories=False,
            stdin=StringIO(),
            stdout=StringIO(),
        )


def test_run_setup_bad_preset_raises(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="default.*external.*custom"):
        run_setup(
            output=tmp_path / "c.yaml",
            non_interactive=True,
            preset="kubernetes",
            skip_hf_token=True,
            hf_token_file=str(tmp_path / "hf"),
            create_directories=False,
            stdin=StringIO(),
            stdout=StringIO(),
        )


async def test_setup_non_interactive_does_not_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[object] = []
    monkeypatch.setattr(
        "mobyai.setup.pull_hub_models",
        lambda *args, **kwargs: called.append(1),
    )
    dest = tmp_path / "config.yaml"
    argv = [
        "setup",
        "--non-interactive",
        "--output",
        str(dest),
        "--workspace",
        str(tmp_path / "projects"),
        "--database",
        str(tmp_path / "mobyai.db"),
        "--hf-token-file",
        str(tmp_path / "secrets" / "hf_token"),
        "--skip-hf-token",
        "--no-directories",
    ]
    code, _out, err = await _setup(argv)
    assert err == ""
    assert code == 0
    assert called == []


async def test_setup_download_models_pulls_unique_hub_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "config.yaml"
    seen: list[tuple[tuple[str, ...], str | None]] = []

    def fake_pull(repo_ids, *, token, stdout, snapshot=None):
        seen.append((tuple(repo_ids), token))
        print(f"Downloading {repo_ids[0]}", file=stdout)
        return ["/cache/fake"]

    monkeypatch.setattr("mobyai.setup.pull_hub_models", fake_pull)
    code, out, err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--output",
            str(dest),
            "--workspace",
            str(tmp_path / "projects"),
            "--database",
            str(tmp_path / "mobyai.db"),
            "--hf-token-file",
            str(tmp_path / "secrets" / "hf_token"),
            "--hf-token",
            "hf_download_secret",
            "--download-models",
            "--skip-runtime",
            "--skip-endpoint-test",
            "--no-directories",
            "--planner-model",
            "Qwen/Qwen3-32B",
            "--orchestrator-model",
            "Qwen/Qwen2.5-7B-Instruct",
            "--worker-model",
            "Qwen/Qwen3-32B",
        ]
    )
    assert err == ""
    assert code == 0
    assert seen == [
        (
            ("Qwen/Qwen3-32B", "Qwen/Qwen2.5-7B-Instruct"),
            "hf_download_secret",
        )
    ]
    assert "Downloading Qwen/Qwen3-32B" in out


async def test_setup_download_failure_is_setup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mobyai.inference.huggingface import HubDownloadError

    monkeypatch.setattr(
        "mobyai.setup.pull_hub_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HubDownloadError("Failed to download Qwen/Qwen3-32B: 401")
        ),
    )
    dest = tmp_path / "config.yaml"
    code, _out, err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--output",
            str(dest),
            "--workspace",
            str(tmp_path / "projects"),
            "--database",
            str(tmp_path / "mobyai.db"),
            "--hf-token-file",
            str(tmp_path / "secrets" / "hf_token"),
            "--hf-token",
            "hf_bad",
            "--download-models",
            "--skip-runtime",
            "--skip-endpoint-test",
            "--no-directories",
        ]
    )
    assert code == 1
    assert "Failed to download" in err
    assert dest.is_file()


async def test_setup_download_models_installs_llama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: list[object] = []
    monkeypatch.setattr(
        "mobyai.setup.ensure_llama_runtime",
        lambda **kwargs: installed.append(kwargs) or tmp_path / "llama",
    )
    monkeypatch.setattr(
        "mobyai.setup.pull_hub_models",
        lambda *args, **kwargs: ["/cache"],
    )
    dest = tmp_path / "config.yaml"
    code, out, err = await _setup(
        [
            "setup",
            "--non-interactive",
            "--output",
            str(dest),
            "--workspace",
            str(tmp_path / "projects"),
            "--database",
            str(tmp_path / "mobyai.db"),
            "--hf-token-file",
            str(tmp_path / "secrets" / "hf_token"),
            "--skip-hf-token",
            "--download-models",
            "--skip-endpoint-test",
            "--no-directories",
        ]
    )
    assert err == ""
    assert code == 0
    assert installed
    assert "llama.cpp" in out or installed

from __future__ import annotations

from pathlib import Path

import pytest

from mobyai.inference.huggingface import (
    huggingface_environ,
    mask_token,
    resolve_huggingface_token,
    token_from_file,
    write_token_file,
)
from mobyai.models.config import AppConfig


def test_write_and_read_token_file(tmp_path: Path) -> None:
    path = write_token_file(tmp_path / "nested" / "hf_token", "  hf_abc  ")
    assert token_from_file(path) == "hf_abc"
    assert path.stat().st_mode & 0o777 == 0o600


def test_resolve_prefers_env(tmp_path: Path, monkeypatch) -> None:
    path = write_token_file(tmp_path / "hf_token", "from-file")
    monkeypatch.setenv("HF_TOKEN", "from-env")
    config = AppConfig.model_validate(
        {
            "project": {"workspace_root": str(tmp_path)},
            "models": {
                "planner": {
                    "model": "a",
                    "endpoint": "http://127.0.0.1:1",
                },
                "orchestrator": {
                    "model": "b",
                    "endpoint": "http://127.0.0.1:2",
                },
                "worker": {
                    "model": "c",
                    "endpoint": "http://127.0.0.1:3",
                },
            },
            "huggingface": {"token_file": str(path)},
        }
    )
    assert resolve_huggingface_token(config) == "from-env"
    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    assert resolve_huggingface_token(config) == "from-file"
    env = huggingface_environ(config)
    assert env["HF_TOKEN"] == "from-file"


def test_mask_token() -> None:
    assert mask_token("abcd") == "****"
    assert mask_token("hf_secret_token").endswith("oken")


def test_hub_repo_ids_unique_and_skips_local_names() -> None:
    from mobyai.inference.huggingface import hub_repo_ids, is_hub_repo_id

    assert is_hub_repo_id("Qwen/Qwen3-32B")
    assert is_hub_repo_id("Qwen/Qwen3-32B-GGUF:Q4_K_M")
    assert not is_hub_repo_id("plan-id")
    assert not is_hub_repo_id("configurable-planner-model")
    assert not is_hub_repo_id("https://example.com/model")
    assert hub_repo_ids(
        "Qwen/Qwen3-32B-GGUF:Q4_K_M",
        "Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        "Qwen/Qwen3-32B-GGUF:Q4_K_M",
        "local-id",
    ) == ["Qwen/Qwen3-32B-GGUF:Q4_K_M", "Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"]


def test_pull_hub_models_uses_snapshot_and_wraps_errors(tmp_path: Path) -> None:
    from io import StringIO

    from mobyai.inference.huggingface import HubDownloadError, pull_hub_models

    seen: list[tuple[str, object]] = []

    def fake_snapshot(repo_id, *, token=None, allow_patterns=None):
        seen.append((repo_id, allow_patterns))
        return str(tmp_path / repo_id.replace("/", "--"))

    stdout = StringIO()
    paths = pull_hub_models(
        ["Qwen/Qwen3-32B-GGUF:Q4_K_M"],
        token="hf_x",
        stdout=stdout,
        snapshot=fake_snapshot,
    )
    assert paths == [str(tmp_path / "Qwen--Qwen3-32B-GGUF")]
    assert seen == [("Qwen/Qwen3-32B-GGUF", ["*Q4_K_M*"])]
    assert "Downloading Qwen/Qwen3-32B-GGUF:Q4_K_M" in stdout.getvalue()

    with pytest.raises(HubDownloadError, match="gated"):
        pull_hub_models(
            ["org/gated"],
            token=None,
            stdout=StringIO(),
            snapshot=lambda repo_id, token=None: (_ for _ in ()).throw(
                RuntimeError("gated")
            ),
        )

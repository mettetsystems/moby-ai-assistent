from __future__ import annotations

from pathlib import Path

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

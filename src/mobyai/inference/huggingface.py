"""Hugging Face auth for weight downloads. The token never lives in YAML."""

from __future__ import annotations

import os
from pathlib import Path

from mobyai.models.config import AppConfig

_ENV_KEYS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


def token_from_file(path: Path | str | None) -> str | None:
    if path is None:
        return None
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return None
    text = file_path.read_text(encoding="utf-8").strip()
    return text or None


def resolve_huggingface_token(config: AppConfig | None = None) -> str | None:
    for key in _ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    if config is not None:
        return token_from_file(config.huggingface.token_file)
    return None


def huggingface_environ(config: AppConfig | None = None) -> dict[str, str]:
    token = resolve_huggingface_token(config)
    if not token:
        return {}
    return {key: token for key in _ENV_KEYS}


def write_token_file(path: Path | str, token: str) -> Path:
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(token.strip() + "\n", encoding="utf-8")
    dest.chmod(0o600)
    return dest


def mask_token(token: str) -> str:
    stripped = token.strip()
    if len(stripped) <= 4:
        return "****"
    return f"...{stripped[-4:]}"

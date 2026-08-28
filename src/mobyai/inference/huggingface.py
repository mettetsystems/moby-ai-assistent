"""Hugging Face auth and Hub weight downloads. The token never lives in YAML."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from mobyai.models.config import AppConfig

_ENV_KEYS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


class HubDownloadError(Exception):
    """Raised when a Hub snapshot cannot be fetched."""


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


def parse_hub_spec(model: str) -> tuple[str, str | None]:
    """Split `org/name:QUANT` used by llama.cpp `-hf` into repo + quant tag."""
    text = model.strip()
    if ":" in text and "://" not in text:
        repo, _, quant = text.rpartition(":")
        if _is_org_name(repo) and quant:
            return repo, quant
    return text, None


def _is_org_name(text: str) -> bool:
    if not text or "://" in text or text.startswith("/"):
        return False
    parts = text.split("/")
    return len(parts) == 2 and all(parts)


def is_hub_repo_id(model: str) -> bool:
    return _is_org_name(parse_hub_spec(model)[0])


def hub_repo_ids(*models: str) -> list[str]:
    seen: list[str] = []
    for model in models:
        spec = model.strip()
        if is_hub_repo_id(spec) and spec not in seen:
            seen.append(spec)
    return seen


def snapshot_hub_repo(
    repo_id: str,
    *,
    token: str | None = None,
    allow_patterns: Sequence[str] | None = None,
) -> str:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise HubDownloadError(
            "huggingface_hub is required to download models. "
            'Install with: pip install -e ".[dev]"'
        ) from exc
    kwargs: dict[str, object] = {"repo_id": repo_id, "token": token}
    if allow_patterns:
        kwargs["allow_patterns"] = list(allow_patterns)
    return str(snapshot_download(**kwargs))


def pull_hub_models(
    repo_ids: Sequence[str],
    *,
    token: str | None,
    stdout: TextIO,
    snapshot: Callable[..., str] | None = None,
) -> list[str]:
    fetch = snapshot or snapshot_hub_repo
    paths: list[str] = []
    total = len(repo_ids)
    for index, spec in enumerate(repo_ids, start=1):
        repo, quant = parse_hub_spec(spec)
        print(f"Downloading {spec}  ({index}/{total})", file=stdout, flush=True)
        patterns = [f"*{quant}*"] if quant else None
        try:
            path = fetch(repo, token=token, allow_patterns=patterns)
        except HubDownloadError:
            raise
        except TypeError:
            try:
                path = fetch(repo, token=token)
            except HubDownloadError:
                raise
            except Exception as exc:
                raise HubDownloadError(f"Failed to download {spec}: {exc}") from exc
        except Exception as exc:
            raise HubDownloadError(f"Failed to download {spec}: {exc}") from exc
        print(f"  cached  {path}", file=stdout)
        paths.append(path)
    return paths

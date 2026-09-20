"""Live operator settings. Overlay YAML keeps comments in the main config intact."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from mobyai.models.config import AgentRole, AppConfig, SamplingConfig, overlay_path_for


def serialize_role_settings(config: AppConfig) -> dict[str, Any]:
    roles: dict[str, Any] = {}
    for role in AgentRole:
        model = config.models.for_role(role)
        sampling = model.sampling
        roles[role.value] = {
            "endpoint": model.endpoint,
            "model": model.model,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
            "engine": model.engine,
            "provider": model.provider,
        }
    return {"services_mode": config.services.mode.value, "models": roles, "final_fail":config.final_fail.model_dump(mode="json")}


def apply_role_settings(config: AppConfig, payload: dict[str, Any]) -> None:
    models = payload.get("models")
    if not isinstance(models, dict):
        raise ValueError("settings.models must be a mapping of roles")
    if 'final_fail' in payload:
        from mobyai.models.config import FinalFailConfig
        config.final_fail = FinalFailConfig.model_validate(payload['final_fail'])
    for role in AgentRole:
        incoming = models.get(role.value)
        if not isinstance(incoming, dict):
            continue
        target = config.models.for_role(role)
        if "endpoint" in incoming:
            endpoint = str(incoming["endpoint"]).strip()
            if not endpoint:
                raise ValueError(f"{role.value} endpoint must be non-empty")
            target.endpoint = endpoint.rstrip("/")
        if "model" in incoming:
            name = str(incoming["model"]).strip()
            if not name:
                raise ValueError(f"{role.value} model must be non-empty")
            target.model = name
        sampling_payload = {
            key: incoming[key]
            for key in ("temperature", "top_p", "top_k")
            if key in incoming
        }
        nested = incoming.get("sampling")
        if isinstance(nested, dict):
            sampling_payload.update(
                {key: nested[key] for key in ("temperature", "top_p", "top_k") if key in nested}
            )
        if sampling_payload:
            data = target.sampling.model_dump()
            data.update(sampling_payload)
            if data.get("top_k") == "" or data.get("top_k") is None:
                data["top_k"] = None
            if data.get("top_p") == "":
                data["top_p"] = None
            target.sampling = SamplingConfig.model_validate(data)


def save_settings_overlay(config_path: Path, config: AppConfig) -> Path:
    dest = overlay_path_for(config_path)
    payload: dict[str, Any] = yaml.safe_load(dest.read_text(encoding="utf-8")) if dest.is_file() else {}
    if not isinstance(payload, dict):
        payload = {}
    payload["final_fail"] = config.final_fail.model_dump(mode="json")
    payload.setdefault("models", {})
    for role in AgentRole:
        model = config.models.for_role(role)
        payload["models"].setdefault(role.value, {}).update({
            "endpoint": model.endpoint,
            "model": model.model,
            "sampling": model.sampling.model_dump(),
        })
    dest.write_text(
        "# Operator overlay written by the MobyAI console. Merged over the main YAML.\n"
        + yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    return dest


class OverlayWatcher:
    """Reload operator overlay when the file changes so a running harness picks up UI saves."""

    def __init__(self, path: Path | None, config: AppConfig) -> None:
        self.path = path
        self.config = config
        self._mtime: float | None = None

    def maybe_reload(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        mtime = self.path.stat().st_mtime
        if self._mtime is not None and mtime <= self._mtime:
            return
        overlay = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(overlay, dict):
            return
        # An overlay can contain only tracing/runtime settings; the role-settings
        # API still requires models, but the general configuration overlay does not.
        if "models" in overlay:
            apply_role_settings(self.config, overlay)
        self._mtime = mtime

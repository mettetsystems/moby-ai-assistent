"""Application configuration loaded from YAML."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentRole(StrEnum):
    PLANNER = "planner"
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"


class ServiceMode(StrEnum):
    EXTERNAL = "external"
    MANAGED = "managed"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InferenceParams(_StrictModel):
    tensor_parallel_size: int = 1
    max_model_len: int | None = None
    kv_cache_dtype: str | None = None
    gpu_memory_utilization: float | None = None
    enable_chunked_prefill: bool = False
    max_num_seqs: int | None = None


class SamplingConfig(_StrictModel):
    """Decoding knobs. Operators may change these live from the console UI."""

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float | None = Field(default=1.0, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)


class RoleModelConfig(_StrictModel):
    provider: str = "openai_compatible"
    engine: str | None = None
    model: str
    gpu: int | None = None
    endpoint: str
    quantization: str | None = None
    kv_cache_dtype: str | None = None
    inference: InferenceParams | None = None
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    launch: list[str] = Field(default_factory=list)


class ModelsConfig(_StrictModel):
    planner: RoleModelConfig
    orchestrator: RoleModelConfig
    worker: RoleModelConfig

    def for_role(self, role: AgentRole) -> RoleModelConfig:
        return getattr(self, role.value)


class HardwareRoleConfig(_StrictModel):
    device: int | str


class HardwareConfig(_StrictModel):
    planner: HardwareRoleConfig
    orchestrator: HardwareRoleConfig
    worker: HardwareRoleConfig

    def for_role(self, role: AgentRole) -> HardwareRoleConfig:
        return getattr(self, role.value)


class ProjectSettings(_StrictModel):
    workspace_root: Path
    extra_allowed_paths: list[Path] = Field(default_factory=list)

    @field_validator("workspace_root", mode="before")
    @classmethod
    def expand_workspace_root(cls, value: Path | str) -> Path:
        return Path(str(value)).expanduser()

    @field_validator("extra_allowed_paths", mode="before")
    @classmethod
    def expand_extra_allowed_paths(cls, value: list[Path | str] | None) -> list[Path]:
        if not value:
            return []
        return [Path(str(item)).expanduser() for item in value]


class RuntimeConfig(_StrictModel):
    max_task_attempts: int = Field(default=2, ge=1)
    command_timeout_seconds: int = Field(default=600, ge=1)
    checkpointing: bool = True
    max_output_bytes: int = Field(default=1_048_576, ge=1024)
    max_log_bytes: int = Field(default=52_428_800, ge=1024)
    max_schema_repair_attempts: int = Field(default=2, ge=0)
    inference_timeout_seconds: int = Field(default=120, ge=1)
    max_worker_turns: int = Field(default=8, ge=1)


class ContextConfig(_StrictModel):
    worker_token_budget: int = Field(default=48000, ge=1)
    log_tail_lines: int = Field(default=150, ge=1)


class InferenceServicesConfig(_StrictModel):
    """Process lifecycle. External mode never starts or stops servers."""

    mode: ServiceMode = ServiceMode.EXTERNAL
    health_timeout_seconds: float = Field(default=60, ge=0.05)
    health_poll_interval_seconds: float = Field(default=0.5, ge=0.01)
    stop_timeout_seconds: float = Field(default=15, ge=0.05)
    swap_shared_device: bool = True


class PersistenceConfig(_StrictModel):
    database_path: Path = Path("~/MobyAI/mobyai.db")

    @field_validator("database_path", mode="before")
    @classmethod
    def expand_database_path(cls, value: Path | str) -> Path:
        return Path(str(value)).expanduser()


class HuggingFaceConfig(_StrictModel):
    """Where the operator token lives. Never store the token string in YAML."""

    token_file: Path | None = None

    @field_validator("token_file", mode="before")
    @classmethod
    def expand_token_file(cls, value: Path | str | None) -> Path | None:
        if value is None or str(value).strip() == "":
            return None
        return Path(str(value)).expanduser()


def overlay_path_for(config_path: Path | str) -> Path:
    path = Path(config_path)
    return path.with_name(f"{path.stem}.overlay.yaml")


def merge_config_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


class AppConfig(_StrictModel):
    project: ProjectSettings
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    services: InferenceServicesConfig = Field(default_factory=InferenceServicesConfig)
    models: ModelsConfig
    hardware: HardwareConfig | None = None
    huggingface: HuggingFaceConfig = Field(default_factory=HuggingFaceConfig)

    @classmethod
    def from_yaml_text(cls, text: str, *, source: str = "<string>") -> AppConfig:
        raw: Any = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ValueError(f"Configuration root must be a mapping: {source}")
        return cls.model_validate(raw)

    @classmethod
    def from_yaml(cls, path: Path | str) -> AppConfig:
        config_path = Path(path)
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Configuration root must be a mapping: {config_path}")
        overlay_file = overlay_path_for(config_path)
        if overlay_file.is_file():
            overlay = yaml.safe_load(overlay_file.read_text(encoding="utf-8")) or {}
            if not isinstance(overlay, dict):
                raise ValueError(f"Overlay root must be a mapping: {overlay_file}")
            raw = merge_config_dicts(raw, overlay)
        return cls.model_validate(raw)

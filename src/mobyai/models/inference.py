"""Inference request/response types. Independent of any serving engine."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mobyai.models.config import AgentRole


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)
    json_mode: bool = True
    response_schema: dict[str, Any] | None = None
    response_schema_name: str = "result"


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    reasoning_content: str | None = None
    model: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class GpuServiceMode(StrEnum):
    IDLE = "idle"
    PLANNER_MODE = "planner"
    WORKER_MODE = "worker"
    ORCHESTRATOR_MODE = "orchestrator"


class InferenceEndpoint(BaseModel):
    """Resolved endpoint for one agent role. Device numbers come from config."""

    model_config = ConfigDict(extra="forbid")

    role: AgentRole
    base_url: str
    model: str
    device: int | str | None = None
    managed: bool = False
    gpu_mode: GpuServiceMode = GpuServiceMode.IDLE

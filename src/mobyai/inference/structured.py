"""Parse model text into Pydantic contracts. Invalid JSON is never accepted silently."""

from __future__ import annotations

from mobyai.tracing import traced

import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from mobyai.inference.base import (
    InferenceProvider,
    SchemaRepairExhaustedError,
    StructuredOutputError,
)
from mobyai.inference.recovery import current_stage, recovery_stage, sampling_steps
from mobyai.models.config import RoleModelConfig
from mobyai.models.inference import ChatMessage, ModelRequest

T = TypeVar("T", bound=BaseModel)

_REPAIR_INSTRUCTION = (
    "Your previous reply was not valid structured output.\n"
    "Errors:\n{errors}\n\n"
    "Required JSON schema:\n{schema}\n\n"
    "Return ONLY valid JSON that matches the required schema. "
    "Do not include markdown, comments, or explanation."
)


def parse_structured(text: str, schema: type[T]) -> T:
    try:
        value = extract_json_value(text)
    except StructuredOutputError:
        raise
    try:
        return schema.model_validate(value)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"JSON did not match {schema.__name__}: {exc}",
            raw=text,
        ) from exc


def extract_json_value(text: str) -> Any:
    candidate = _strip_fences(text).strip()
    if not candidate:
        raise StructuredOutputError("Model returned empty content", raw=text)
    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(candidate, index)
        except json.JSONDecodeError:
            continue
        return value
    raise StructuredOutputError("No JSON object or array found in model output", raw=text)


class StructuredGenerator:
    """Generate then validate. Repair is bounded and uses validation errors, not guesswork."""

    def __init__(
        self,
        provider: InferenceProvider,
        *,
        max_repair_attempts: int = 2,
        settings: RoleModelConfig | None = None,
        refresh: Callable[[], None] | None = None,
        recovery_enabled: bool = True,
    ) -> None:
        self.provider = provider
        self.max_repair_attempts = max(max_repair_attempts, 0)
        self.settings = settings
        self._refresh = refresh
        self.recovery_enabled = recovery_enabled

    @traced("model.structured")
    async def generate(
        self,
        request: ModelRequest,
        schema: type[T],
    ) -> T:
        request = self._with_live_settings(request)
        if self.settings is not None and self.settings.structured_output == "json_schema":
            schema_json = schema.model_json_schema()
            request = request.model_copy(update={"response_schema": schema_json,
                "response_schema_name": schema.__name__,
                "messages": [*request.messages, ChatMessage(role="user", content=
                    "Output must match this schema, including any plan/patch wrapper. "
                    "Choose either nonempty inspect_calls or a completed plan/patch, not an empty object.\n" +
                    json.dumps(schema_json, ensure_ascii=False))]})
        response = await self._request(request)
        try:
            return parse_structured(response.content, schema)
        except StructuredOutputError as first_error:
            return await self._repair(request, schema, response.content, first_error)

    @traced("model.schema_repair")
    async def _repair(
        self,
        request: ModelRequest,
        schema: type[T],
        previous: str,
        error: StructuredOutputError,
    ) -> T:
        messages = list(request.messages)
        last_error = error
        last_raw = previous
        starting_stage = current_stage()
        for _attempt in range(self.max_repair_attempts):
            messages = [
                *messages,
                ChatMessage(role="assistant", content=last_raw or " "),
                ChatMessage(
                    role="user",
                    content=_REPAIR_INSTRUCTION.format(
                        errors=str(last_error),
                        schema=json.dumps(schema.model_json_schema(), ensure_ascii=False),
                    ),
                ),
            ]
            with recovery_stage(starting_stage + _attempt):
                repaired_request = self._with_live_settings(
                    request.model_copy(update={"messages": messages})
                )
            response = await self._request(repaired_request)
            last_raw = response.content
            try:
                return parse_structured(response.content, schema)
            except StructuredOutputError as repair_error:
                last_error = repair_error
        raise SchemaRepairExhaustedError(
            f"Schema repair failed after {self.max_repair_attempts} attempt(s): {last_error}",
            raw=last_raw,
            attempts=self.max_repair_attempts,
        ) from last_error

    async def _request(self, request):
        from mobyai.tracing import span, attribute
        with span("model.request", model=getattr(self.provider, "model", "unknown"),
                  temperature=request.temperature, top_p=request.top_p,
                  top_k=request.top_k) as active:
            response = await self.provider.generate(request)
            from mobyai.review.minutes import capture_call
            capture_call(request, response)
            attribute(active, "prompt_tokens", response.prompt_tokens)
            attribute(active, "completion_tokens", response.completion_tokens)
            attribute(active, "finish_reason", response.finish_reason)
            return response

    def _with_live_settings(self, request: ModelRequest) -> ModelRequest:
        if self._refresh is not None:
            self._refresh()
        if self.settings is None:
            return request
        sampling = self.settings.sampling
        profiles = sampling_steps(sampling, self.settings.recovery_sampling)
        if self.recovery_enabled and current_stage() and profiles:
            sampling = profiles[min(current_stage(), len(profiles)) - 1]
        provider = self.provider
        endpoint = getattr(provider, "endpoint", None)
        if isinstance(endpoint, str):
            provider.endpoint = self.settings.endpoint.rstrip("/")
        if hasattr(provider, "stream_responses"):
            provider.stream_responses = self.settings.stream_responses
            provider.stream_idle_timeout_seconds = self.settings.stream_idle_timeout_seconds
        if hasattr(provider, "model"):
            provider.model = self.settings.model
        return request.model_copy(
            update={
                "temperature": sampling.temperature,
                "top_p": sampling.top_p,
                "top_k": sampling.top_k,
            }
        )


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)

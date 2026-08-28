"""Parse model text into Pydantic contracts. Invalid JSON is never accepted silently."""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from mobyai.inference.base import (
    InferenceProvider,
    SchemaRepairExhaustedError,
    StructuredOutputError,
)
from mobyai.models.inference import ChatMessage, ModelRequest

T = TypeVar("T", bound=BaseModel)

_REPAIR_INSTRUCTION = (
    "Your previous reply was not valid structured output.\n"
    "Errors:\n{errors}\n\n"
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
    ) -> None:
        self.provider = provider
        self.max_repair_attempts = max(max_repair_attempts, 0)

    async def generate(
        self,
        request: ModelRequest,
        schema: type[T],
    ) -> T:
        response = await self.provider.generate(request)
        try:
            return parse_structured(response.content, schema)
        except StructuredOutputError as first_error:
            return await self._repair(request, schema, response.content, first_error)

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
        for _attempt in range(self.max_repair_attempts):
            messages = [
                *messages,
                ChatMessage(role="assistant", content=last_raw or " "),
                ChatMessage(
                    role="user",
                    content=_REPAIR_INSTRUCTION.format(errors=str(last_error)),
                ),
            ]
            repaired_request = request.model_copy(update={"messages": messages})
            response = await self.provider.generate(repaired_request)
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

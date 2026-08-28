"""Inference provider protocol. Serving engines stay behind this boundary."""

from __future__ import annotations

from typing import Protocol

from mobyai.models.inference import ModelRequest, ModelResponse


class InferenceError(Exception):
    """The provider could not complete a generate call."""


class UnknownProviderError(InferenceError):
    """Configured provider name is not supported."""


class StructuredOutputError(Exception):
    """Model text could not be parsed into the required schema."""

    def __init__(self, message: str, *, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class SchemaRepairExhaustedError(StructuredOutputError):
    """Bounded repair attempts still produced invalid structured output."""

    def __init__(self, message: str, *, raw: str | None = None, attempts: int = 0) -> None:
        super().__init__(message, raw=raw)
        self.attempts = attempts


class InferenceProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def healthcheck(self) -> bool: ...

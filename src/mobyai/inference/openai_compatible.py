"""OpenAI-compatible chat completions client (vLLM, llama.cpp, Ollama, SGLang)."""

from __future__ import annotations

import asyncio
from typing import Any
from mobyai.tracing import span, attribute

import httpx

from mobyai.inference.base import InferenceError
from mobyai.inference.health import join_url, openai_healthcheck
from mobyai.models.inference import ModelRequest, ModelResponse


class OpenAICompatibleProvider:
    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 600,
        max_transport_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.max_transport_retries = max(0, min(max_transport_retries, 5))
        self._owns_client = client is None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers=headers,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
        }
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.top_k is not None:
            payload["top_k"] = request.top_k
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = await self._post(payload)
        except httpx.HTTPError as exc:
            raise InferenceError(
                f"Inference request failed for model {self.model} at {self.endpoint}: "
                f"{type(exc).__name__}: {str(exc) or 'no response received'}"
            ) from exc
        if response.status_code >= 400:
            snippet = response.text[:500]
            raise InferenceError(
                f"Inference HTTP {response.status_code}: {snippet}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise InferenceError("Inference response was not JSON") from exc
        return _parse_chat_completion(body, fallback_model=self.model)

    async def _post(self, payload):
        for attempt in range(self.max_transport_retries + 1):
            with span("inference.http", attempt=attempt + 1) as active:
                try:
                    response = await self._client.post(
                        join_url(self.endpoint, "/v1/chat/completions"), json=payload)
                    attribute(active, "http.status_code", response.status_code)
                except (httpx.TransportError,) as exc:
                    attribute(active, "error.type", type(exc).__name__)
                    if attempt >= self.max_transport_retries:
                        raise
                else:
                    if response.status_code not in {429, 502, 503, 504} or attempt >= self.max_transport_retries:
                        return response
            # Only retry transient transport/service errors, with unchanged sampling.
            await asyncio.sleep(min(2 ** attempt, 4))
        raise AssertionError("unreachable")

    async def healthcheck(self) -> bool:
        return await openai_healthcheck(self._client, self.endpoint)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_chat_completion(body: dict[str, Any], *, fallback_model: str) -> ModelResponse:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InferenceError("Inference response missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise InferenceError("Inference choice was not an object")
    message = first.get("message") or {}
    content = _message_content(message)
    if not content:
        content = str(first.get("text") or "")
    if not content:
        raise InferenceError("Inference response had empty content")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return ModelResponse(
        content=content,
        reasoning_content=message.get("reasoning_content") if isinstance(message.get("reasoning_content"), str) else None,
        model=str(body.get("model") or fallback_model),
        finish_reason=first.get("finish_reason"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


def _message_content(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""

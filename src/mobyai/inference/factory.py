"""Build inference providers from configuration. Model IDs stay in YAML."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from mobyai.inference.base import InferenceProvider, UnknownProviderError
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.models.config import AgentRole, AppConfig, RoleModelConfig

_OPENAI_COMPATIBLE = frozenset(
    {
        "openai_compatible",
        "vllm",
        "llamacpp",
        "llama.cpp",
        "ollama",
        "sglang",
    }
)


def provider_from_role_config(
    config: RoleModelConfig,
    *,
    timeout_seconds: float = 600,
    client: httpx.AsyncClient | None = None,
) -> InferenceProvider:
    name = config.provider.strip().lower()
    if name in _OPENAI_COMPATIBLE:
        return OpenAICompatibleProvider(
            config.endpoint,
            config.model,
            timeout_seconds=timeout_seconds,
            client=client,
        )
    raise UnknownProviderError(f"Unsupported inference provider: {config.provider}")


class InferenceRegistry:
    def __init__(self, providers: Mapping[AgentRole, InferenceProvider]) -> None:
        self._providers = dict(providers)

    def get(self, role: AgentRole) -> InferenceProvider:
        return self._providers[role]

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> InferenceRegistry:
        timeout = float(config.runtime.inference_timeout_seconds)
        providers = {
            role: provider_from_role_config(
                config.models.for_role(role),
                timeout_seconds=timeout,
                client=client,
            )
            for role in AgentRole
        }
        return cls(providers)

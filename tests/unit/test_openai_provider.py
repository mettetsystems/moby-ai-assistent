import json
from pathlib import Path

import httpx
import pytest

from mobyai.inference.base import InferenceError, UnknownProviderError
from mobyai.inference.factory import InferenceRegistry, provider_from_role_config
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.models.config import AgentRole, AppConfig, RoleModelConfig
from mobyai.models.inference import ChatMessage, ModelRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"

VALID_PLANNER = {
    "project_name": "example_app",
    "architecture_summary": "Small SQLite-backed service.",
    "directory_structure": ["src/"],
    "tasks": [
        {
            "task_id": "STEP_001",
            "name": "Implement database connection",
            "description": "Create a pooled SQLite connection layer.",
            "target_files": ["src/db.py"],
            "test_commands": ["pytest tests/test_db.py"],
        }
    ],
    "documentation_requirements": ["README.md"],
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _completion_handler(
    content: str,
    *,
    status: int = 200,
    health_status: int = 200,
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            if status >= 400:
                return httpx.Response(status, text="nope")
            body = json.loads(request.content.decode("utf-8"))
            assert body["model"] == "test-model"
            assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(
                200,
                json={
                    "model": "test-model",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 8},
                },
            )
        if path.endswith("/models") or path.endswith("/health"):
            return httpx.Response(health_status, json={"data": []})
        return httpx.Response(404)

    return handler


async def test_generate_chat_completion() -> None:
    payload = json.dumps(VALID_PLANNER)
    client = _client(_completion_handler(payload))
    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:9802",
        "test-model",
        client=client,
    )
    response = await provider.generate(
        ModelRequest(messages=[ChatMessage(role="user", content="Plan the app.")])
    )
    assert json.loads(response.content)["project_name"] == "example_app"
    assert response.model == "test-model"
    assert response.prompt_tokens == 4
    await client.aclose()


async def test_healthcheck_true_and_false() -> None:
    healthy = OpenAICompatibleProvider(
        "http://127.0.0.1:9802",
        "test-model",
        client=_client(_completion_handler("{}", health_status=200)),
    )
    sick = OpenAICompatibleProvider(
        "http://127.0.0.1:9802",
        "test-model",
        client=_client(_completion_handler("{}", health_status=503)),
    )
    assert await healthy.healthcheck() is True
    assert await sick.healthcheck() is False


async def test_http_error_becomes_inference_error() -> None:
    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:9802",
        "test-model",
        client=_client(_completion_handler("{}", status=500)),
    )
    with pytest.raises(InferenceError, match="500"):
        await provider.generate(
            ModelRequest(messages=[ChatMessage(role="user", content="Plan the app.")])
        )


async def test_v1_suffix_is_not_doubled() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
            },
        )

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:9802/v1",
        "test-model",
        client=_client(handler),
    )
    await provider.generate(
        ModelRequest(
            messages=[ChatMessage(role="user", content="Plan the app.")],
            json_mode=False,
        )
    )
    assert seen[0] == "http://127.0.0.1:9802/v1/chat/completions"


def test_unknown_provider_is_rejected() -> None:
    config = RoleModelConfig(
        provider="cloud-only-mystery",
        model="x",
        endpoint="http://127.0.0.1:9",
    )
    with pytest.raises(UnknownProviderError):
        provider_from_role_config(config)


async def test_registry_from_config_uses_mock_client() -> None:
    config = AppConfig.from_yaml(DEFAULT_CONFIG)
    client = _client(_completion_handler("{}", health_status=200))
    registry = InferenceRegistry.from_config(config, client=client)
    assert await registry.get(AgentRole.ORCHESTRATOR).healthcheck() is True
    await client.aclose()


async def test_empty_timeout_preserves_diagnostic_identity() -> None:
    import httpx
    import pytest
    from mobyai.inference.openai_compatible import OpenAICompatibleProvider
    from mobyai.inference.base import InferenceError
    from mobyai.models.inference import ModelRequest, ChatMessage

    def timeout(request):
        raise httpx.ReadTimeout("", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        provider = OpenAICompatibleProvider("http://localhost:9802", "planner-model", client=client)
        with pytest.raises(InferenceError) as error:
            await provider.generate(ModelRequest(messages=[ChatMessage(role="user", content="test")]))
        assert "ReadTimeout" in str(error.value)
        assert "localhost:9802" in str(error.value)
        assert "planner-model" in str(error.value)

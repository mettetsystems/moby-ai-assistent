from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import yaml

from mobyai.inference.service_manager import (
    GpuServiceManager,
    RoleUnhealthyError,
    device_for_role,
    render_launch,
    service_manager_from_config,
)
from mobyai.models.config import AgentRole, AppConfig, ServiceMode
from mobyai.models.inference import GpuServiceMode

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"
MANAGED_CONFIG = REPO_ROOT / "config" / "examples" / "managed.yaml"


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class RecordingSpawner:
    def __init__(self) -> None:
        self.starts: list[dict[str, object]] = []
        self.handles: list[FakeProcess] = []

    def spawn(self, argv, *, env, cwd=None):
        handle = FakeProcess(pid=9000 + len(self.handles))
        self.starts.append({"argv": list(argv), "env": dict(env), "cwd": cwd})
        self.handles.append(handle)
        return handle


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _payload(**overrides: object) -> dict[str, object]:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    data["hardware"]["planner"]["device"] = 3
    data["hardware"]["orchestrator"]["device"] = 7
    data["hardware"]["worker"]["device"] = 3
    data["models"]["planner"]["gpu"] = 3
    data["models"]["orchestrator"]["gpu"] = 7
    data["models"]["worker"]["gpu"] = 3
    data["services"]["health_timeout_seconds"] = 0.2
    data["services"]["health_poll_interval_seconds"] = 0.01
    data["services"]["stop_timeout_seconds"] = 0.05
    for key, value in overrides.items():
        if key == "services" and isinstance(value, dict):
            data["services"].update(value)
        else:
            data[key] = value
    return data


def _managed_payload(**overrides: object) -> dict[str, object]:
    launch = ["vllm", "serve", "{model}", "--port", "{port}"]
    extra_services = overrides.pop("services", {})
    data = _payload(services={"mode": "managed"}, **overrides)
    if extra_services:
        data["services"].update(extra_services)
    for role in ("planner", "orchestrator", "worker"):
        data["models"][role]["launch"] = list(launch)
    return data


def _health_after_spawn(config: AppConfig, spawner: RecordingSpawner):
    def health(role: AgentRole) -> bool:
        port = str(urlparse(config.models.for_role(role).endpoint).port)
        for start, handle in zip(spawner.starts, spawner.handles, strict=False):
            if port in start["argv"] and handle.poll() is None:
                return True
        return False

    return health


def _health_client(ok: bool) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        if ok:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(503, text="down")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_default_yaml_is_external() -> None:
    config = AppConfig.from_yaml(DEFAULT_CONFIG)
    assert config.services.mode is ServiceMode.EXTERNAL
    assert config.services.swap_shared_device is True
    assert config.models.worker.launch == []


def test_managed_example_loads() -> None:
    config = AppConfig.from_yaml(MANAGED_CONFIG)
    assert config.services.mode is ServiceMode.MANAGED
    assert "{model}" in config.models.worker.launch
    argv = render_launch(config, AgentRole.WORKER)
    assert config.models.worker.model in argv
    assert "9800" in argv


def test_device_for_role_uses_hardware_not_constants() -> None:
    config = AppConfig.model_validate(_payload())
    assert device_for_role(config, AgentRole.PLANNER) == 3
    assert device_for_role(config, AgentRole.ORCHESTRATOR) == 7
    assert device_for_role(config, AgentRole.WORKER) == 3


async def test_external_ensure_healthchecks_without_spawn() -> None:
    config = AppConfig.model_validate(_payload())
    spawner = RecordingSpawner()
    client = _health_client(True)
    manager = GpuServiceManager(config, client=client, spawner=spawner)
    endpoint = await manager.ensure_role_available(AgentRole.ORCHESTRATOR)
    assert endpoint.base_url == config.models.orchestrator.endpoint
    assert endpoint.managed is False
    assert endpoint.device == 7
    assert manager.gpu_mode(7) is GpuServiceMode.ORCHESTRATOR_MODE
    assert spawner.starts == []
    await manager.stop_role(AgentRole.ORCHESTRATOR)
    assert spawner.handles == []
    await manager.aclose()


async def test_external_unhealthy_raises() -> None:
    config = AppConfig.model_validate(_payload())
    manager = GpuServiceManager(
        config, client=_health_client(False), spawner=RecordingSpawner()
    )
    with pytest.raises(RoleUnhealthyError, match="orchestrator"):
        await manager.ensure_role_available(AgentRole.ORCHESTRATOR)
    await manager.aclose()


async def test_external_ignores_launch_argv() -> None:
    data = _payload()
    data["models"]["worker"]["launch"] = ["vllm", "serve", "{model}"]
    config = AppConfig.model_validate(data)
    spawner = RecordingSpawner()
    manager = GpuServiceManager(config, client=_health_client(True), spawner=spawner)
    await manager.ensure_role_available(AgentRole.WORKER)
    assert spawner.starts == []
    await manager.aclose()


async def test_managed_sets_cuda_visible_devices_from_config() -> None:
    config = AppConfig.model_validate(_managed_payload())
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    endpoint = await manager.ensure_role_available(AgentRole.PLANNER)
    assert endpoint.managed is True
    assert spawner.starts[0]["env"]["CUDA_VISIBLE_DEVICES"] == "3"
    assert "9802" in spawner.starts[0]["argv"]
    assert manager.gpu_mode(3) is GpuServiceMode.PLANNER_MODE
    await manager.aclose()


async def test_managed_isolates_orchestrator_on_other_device() -> None:
    config = AppConfig.model_validate(_managed_payload())
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    await manager.ensure_role_available(AgentRole.PLANNER)
    await manager.ensure_role_available(AgentRole.ORCHESTRATOR)
    assert spawner.starts[1]["env"]["CUDA_VISIBLE_DEVICES"] == "7"
    assert spawner.handles[0].terminated is False
    await manager.aclose()


async def test_persist_then_swap_stops_shared_device_occupant() -> None:
    config = AppConfig.model_validate(_managed_payload())
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    await manager.ensure_role_available(AgentRole.PLANNER)
    await manager.ensure_role_available(AgentRole.WORKER)
    assert spawner.handles[0].terminated is True
    assert spawner.starts[1]["env"]["CUDA_VISIBLE_DEVICES"] == "3"
    assert manager.gpu_mode(3) is GpuServiceMode.WORKER_MODE
    assert len(spawner.starts) == 2
    await manager.aclose()


async def test_swap_disabled_keeps_both_on_shared_device() -> None:
    config = AppConfig.model_validate(
        _managed_payload(services={"swap_shared_device": False})
    )
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    await manager.ensure_role_available(AgentRole.PLANNER)
    await manager.ensure_role_available(AgentRole.WORKER)
    assert spawner.handles[0].terminated is False
    assert len(spawner.starts) == 2
    await manager.aclose()


async def test_managed_does_not_respawn_healthy_process() -> None:
    config = AppConfig.model_validate(_managed_payload())
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    await manager.ensure_role_available(AgentRole.WORKER)
    await manager.ensure_role_available(AgentRole.WORKER)
    assert len(spawner.starts) == 1
    await manager.aclose()


async def test_cpu_device_clears_cuda_visible_devices() -> None:
    data = _managed_payload()
    data["hardware"]["orchestrator"]["device"] = "cpu"
    config = AppConfig.model_validate(data)
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    await manager.ensure_role_available(AgentRole.ORCHESTRATOR)
    assert spawner.starts[0]["env"]["CUDA_VISIBLE_DEVICES"] == ""
    await manager.aclose()


async def test_managed_injects_hf_token_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    token_path = tmp_path / "hf_token"
    token_path.write_text("hf_from_file\n", encoding="utf-8")
    data = _managed_payload()
    data["huggingface"] = {"token_file": str(token_path)}
    config = AppConfig.model_validate(data)
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    await manager.ensure_role_available(AgentRole.PLANNER)
    env = spawner.starts[0]["env"]
    assert env["HF_TOKEN"] == "hf_from_file"
    assert env["HUGGING_FACE_HUB_TOKEN"] == "hf_from_file"
    await manager.aclose()


async def test_managed_empty_launch_is_external() -> None:
    data = _managed_payload()
    data["models"]["worker"]["launch"] = []
    config = AppConfig.model_validate(data)
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, client=_health_client(True), spawner=spawner
    )
    endpoint = await manager.ensure_role_available(AgentRole.WORKER)
    assert endpoint.managed is False
    assert spawner.starts == []
    await manager.aclose()


async def test_health_timeout_stops_unhealthy_spawn() -> None:
    config = AppConfig.model_validate(_managed_payload())
    spawner = RecordingSpawner()
    clock = Clock()
    manager = GpuServiceManager(
        config,
        spawner=spawner,
        health=lambda _role: False,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    with pytest.raises(RoleUnhealthyError, match="healthy"):
        await manager.ensure_role_available(AgentRole.WORKER)
    assert spawner.handles[0].terminated is True
    await manager.aclose()


async def test_stop_role_terminates_managed_process() -> None:
    config = AppConfig.model_validate(_managed_payload())
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=_health_after_spawn(config, spawner)
    )
    await manager.ensure_role_available(AgentRole.WORKER)
    await manager.stop_role(AgentRole.WORKER)
    assert spawner.handles[0].terminated is True
    assert manager.gpu_mode(3) is GpuServiceMode.IDLE
    await manager.aclose()


async def test_managed_reuses_healthy_endpoint_without_spawn() -> None:
    config = AppConfig.model_validate(_managed_payload())
    spawner = RecordingSpawner()
    manager = GpuServiceManager(
        config, spawner=spawner, health=lambda _role: True
    )
    endpoint = await manager.ensure_role_available(AgentRole.WORKER)
    assert endpoint.managed is True
    assert spawner.starts == []
    await manager.aclose()


async def test_factory_builds_from_config() -> None:
    config = AppConfig.from_yaml(DEFAULT_CONFIG)
    manager = service_manager_from_config(
        config, client=_health_client(True), spawner=RecordingSpawner()
    )
    assert isinstance(manager, GpuServiceManager)
    await manager.aclose()

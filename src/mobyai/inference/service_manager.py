"""GPU / inference process lifecycle. Isolated from agent prompts and DAG logic."""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from mobyai.inference.base import InferenceError
from mobyai.inference.health import openai_healthcheck
from mobyai.inference.huggingface import huggingface_environ
from mobyai.inference.llamacpp import resolve_launch_argv
from mobyai.models.config import AgentRole, AppConfig, ServiceMode
from mobyai.models.inference import GpuServiceMode, InferenceEndpoint

HealthProbe = Callable[[AgentRole], bool | Awaitable[bool]]
RoleProgressFn = Callable[[AgentRole, str], None]
SleepFn = Callable[[float], Awaitable[None]]
MonotonicFn = Callable[[], float]
_PROGRESS_INTERVAL_SECONDS = 10.0


_GPU_MODE = {
    AgentRole.PLANNER: GpuServiceMode.PLANNER_MODE,
    AgentRole.WORKER: GpuServiceMode.WORKER_MODE,
    AgentRole.ORCHESTRATOR: GpuServiceMode.ORCHESTRATOR_MODE,
}
_CPU_DEVICES = frozenset({"cpu", "none", "-1", ""})


class ServiceManagerError(InferenceError):
    """Could not make a role's inference endpoint available."""


class RoleUnhealthyError(ServiceManagerError):
    """Configured endpoint did not become healthy in time."""


class ProcessSpawnError(ServiceManagerError):
    """Managed process could not be started."""


class ManagedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ProcessSpawner(Protocol):
    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None = None,
        log_file: Path | None = None,
    ) -> ManagedProcess: ...


class InferenceServiceManager(Protocol):
    async def ensure_role_available(self, role: AgentRole) -> InferenceEndpoint: ...

    async def healthcheck(self, role: AgentRole) -> bool: ...

    async def stop_role(self, role: AgentRole) -> None: ...


class SubprocessManagedProcess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        pid = self._process.pid
        if pid is None:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self._process.terminate()

    def kill(self) -> None:
        pid = self._process.pid
        if pid is None:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            self._process.kill()

    def wait(self, timeout: float | None = None) -> int:
        return int(self._process.wait(timeout=timeout))


class SubprocessSpawner:
    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None = None,
        log_file: Path | None = None,
    ) -> ManagedProcess:
        stderr_handle = None
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            stderr_handle = log_file.open("ab")
        try:
            process = subprocess.Popen(
                list(argv),
                env=dict(env),
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle if stderr_handle is not None else subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProcessSpawnError(f"Failed to spawn {argv[0]}: {exc}") from exc
        finally:
            if stderr_handle is not None:
                stderr_handle.close()
        return SubprocessManagedProcess(process)


class GpuServiceManager:
    """External endpoints by default. Managed mode starts isolated CUDA processes."""

    def __init__(
        self,
        config: AppConfig,
        *,
        client: httpx.AsyncClient | None = None,
        spawner: ProcessSpawner | None = None,
        health: HealthProbe | None = None,
        progress: RoleProgressFn | None = None,
        sleep: SleepFn | None = None,
        monotonic: MonotonicFn | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        timeout = float(config.runtime.inference_timeout_seconds)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._spawner = spawner or SubprocessSpawner()
        self._health_probe = health
        self._progress = progress
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._lock = asyncio.Lock()
        self._processes: dict[AgentRole, ManagedProcess] = {}
        self._occupancy: dict[str, AgentRole] = {}
        self._log_dir = _service_log_dir(config)

    def set_progress(self, progress: RoleProgressFn | None) -> None:
        self._progress = progress

    async def ensure_role_available(self, role: AgentRole) -> InferenceEndpoint:
        async with self._lock:
            return await self._ensure(role)

    async def healthcheck(self, role: AgentRole) -> bool:
        return await self._probe_health(role)

    async def stop_role(self, role: AgentRole) -> None:
        async with self._lock:
            await self._stop(role)

    def gpu_mode(self, device: int | str | None) -> GpuServiceMode:
        key = _device_key(device)
        if key is None:
            return GpuServiceMode.IDLE
        occupant = self._occupancy.get(key)
        if occupant is None:
            return GpuServiceMode.IDLE
        return _GPU_MODE[occupant]

    async def aclose(self) -> None:
        async with self._lock:
            for role in list(self._processes):
                await self._stop(role)
        if self._owns_client:
            await self._client.aclose()

    async def _ensure(self, role: AgentRole) -> InferenceEndpoint:
        model = self._config.models.for_role(role)
        device = self._device(role)
        self._report(role, f"check   {model.endpoint}  device={device}")

        if await self._probe_health(role):
            self._report(role, "reuse   endpoint already healthy")
            self._occupy(role)
            return self._endpoint(role)

        if not self._is_managed(role):
            raise RoleUnhealthyError(
                f"{role.value} endpoint is not healthy: "
                f"{self._config.models.for_role(role).endpoint}"
            )

        if self._alive(role):
            await self._stop(role)

        if self._config.services.swap_shared_device:
            await self._vacate_shared_device(role)

        self._report(role, "launch  starting llama.cpp server")
        await self._start(role)
        try:
            await self._wait_healthy(role)
        except RoleUnhealthyError:
            await self._stop(role)
            raise
        self._occupy(role)
        return self._endpoint(role)

    async def _start(self, role: AgentRole) -> None:
        argv = render_launch(self._config, role)
        if not argv:
            raise ProcessSpawnError(f"{role.value} has no launch argv")
        env = _launch_env(self._device(role), extra=huggingface_environ(self._config))
        log_file = self._log_dir / f"{role.value}.log" if self._log_dir is not None else None
        handle = self._spawner.spawn(argv, env=env, log_file=log_file)
        self._processes[role] = handle
        model = self._config.models.for_role(role).model
        log_detail = f"  log={log_file}" if log_file is not None else ""
        self._report(role, f"spawn   pid={handle.pid}  model={model}{log_detail}")

    async def _stop(self, role: AgentRole) -> None:
        handle = self._processes.pop(role, None)
        self._release(role)
        if handle is None or not self._is_managed(role):
            return
        if handle.poll() is not None:
            return
        timeout = float(self._config.services.stop_timeout_seconds)
        handle.terminate()
        try:
            handle.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            handle.kill()
            handle.wait(timeout=min(timeout, 5.0))

    async def _vacate_shared_device(self, keep: AgentRole) -> None:
        device = self._device(keep)
        if device is None or _is_cpu(device):
            return
        key = _device_key(device)
        for other in AgentRole:
            if other is keep:
                continue
            if _device_key(self._device(other)) != key:
                continue
            if self._alive(other) or self._occupancy.get(key) is other:
                await self._stop(other)

    async def _wait_healthy(self, role: AgentRole) -> None:
        timeout = float(self._config.services.health_timeout_seconds)
        interval = float(self._config.services.health_poll_interval_seconds)
        started = self._monotonic()
        deadline = started + timeout
        last_report = -_PROGRESS_INTERVAL_SECONDS
        endpoint = self._config.models.for_role(role).endpoint
        while True:
            if not self._alive(role):
                code = self._exit_code(role)
                log_hint = ""
                if self._log_dir is not None:
                    log_hint = f" (see {self._log_dir / f'{role.value}.log'})"
                raise RoleUnhealthyError(
                    f"{role.value} process exited with code {code} before becoming healthy{log_hint}"
                )
            if await self._probe_health(role):
                elapsed = self._monotonic() - started
                self._report(role, f"healthy ready after {elapsed:.0f}s")
                return
            now = self._monotonic()
            if now >= deadline:
                raise RoleUnhealthyError(
                    f"{role.value} did not become healthy within {timeout}s"
                )
            elapsed = now - started
            if elapsed - last_report >= _PROGRESS_INTERVAL_SECONDS:
                self._report(
                    role,
                    f"wait    probing {endpoint} ({elapsed:.0f}s elapsed, model may still be loading)",
                )
                last_report = elapsed
            await self._sleep(interval)

    def _report(self, role: AgentRole, detail: str) -> None:
        if self._progress is not None:
            self._progress(role, detail)

    async def _probe_health(self, role: AgentRole) -> bool:
        if self._health_probe is not None:
            result = self._health_probe(role)
            if inspect.isawaitable(result):
                return bool(await result)
            return bool(result)
        endpoint = self._config.models.for_role(role).endpoint
        return await openai_healthcheck(self._client, endpoint)

    def _is_managed(self, role: AgentRole) -> bool:
        if self._config.services.mode is not ServiceMode.MANAGED:
            return False
        return bool(self._config.models.for_role(role).launch)

    def _alive(self, role: AgentRole) -> bool:
        handle = self._processes.get(role)
        return handle is not None and handle.poll() is None

    def _exit_code(self, role: AgentRole) -> int | None:
        handle = self._processes.get(role)
        if handle is None:
            return None
        return handle.poll()

    def _device(self, role: AgentRole) -> int | str | None:
        return device_for_role(self._config, role)

    def _occupy(self, role: AgentRole) -> None:
        key = _device_key(self._device(role))
        if key is None:
            return
        self._occupancy[key] = role

    def _release(self, role: AgentRole) -> None:
        key = _device_key(self._device(role))
        if key is not None and self._occupancy.get(key) is role:
            del self._occupancy[key]

    def _endpoint(self, role: AgentRole) -> InferenceEndpoint:
        model = self._config.models.for_role(role)
        device = self._device(role)
        return InferenceEndpoint(
            role=role,
            base_url=model.endpoint,
            model=model.model,
            device=device,
            managed=self._is_managed(role),
            gpu_mode=_GPU_MODE[role],
        )


def device_for_role(config: AppConfig, role: AgentRole) -> int | str | None:
    if config.hardware is not None:
        return config.hardware.for_role(role).device
    return config.models.for_role(role).gpu


def render_launch(config: AppConfig, role: AgentRole) -> list[str]:
    model = config.models.for_role(role)
    values = {
        "model": model.model,
        "endpoint": model.endpoint,
        "port": _endpoint_port(model.endpoint),
        "device": "" if device_for_role(config, role) is None else str(device_for_role(config, role)),
        "quantization": model.quantization or "",
    }
    return resolve_launch_argv(
        [_substitute(part, values) for part in model.launch]
    )


def service_manager_from_config(
    config: AppConfig,
    *,
    client: httpx.AsyncClient | None = None,
    spawner: ProcessSpawner | None = None,
    health: HealthProbe | None = None,
) -> GpuServiceManager:
    return GpuServiceManager(
        config, client=client, spawner=spawner, health=health
    )


def _launch_env(
    device: int | str | None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    # Match nvidia-smi indices. Without this, CUDA often uses FASTEST_FIRST so
    # CUDA_VISIBLE_DEVICES=1 can select the smaller card on a dual-GPU box.
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    if device is None or _is_cpu(device):
        env["CUDA_VISIBLE_DEVICES"] = ""
    else:
        env["CUDA_VISIBLE_DEVICES"] = str(device)
    if extra:
        env.update(extra)
        # Keep PCI_BUS_ID even if extras try to override with an empty value.
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    return env


def _endpoint_port(url: str) -> str:
    parsed = urlparse(url)
    if parsed.port is not None:
        return str(parsed.port)
    if parsed.scheme == "https":
        return "443"
    return "80"


def _substitute(part: str, values: Mapping[str, str]) -> str:
    rendered = part
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _is_cpu(device: int | str | None) -> bool:
    return str(device).strip().lower() in _CPU_DEVICES


def _device_key(device: int | str | None) -> str | None:
    if device is None or _is_cpu(device):
        return None
    return str(device)


def _service_log_dir(config: AppConfig) -> Path | None:
    if config.services.mode is not ServiceMode.MANAGED:
        return None
    return config.persistence.database_path.expanduser().resolve().parent / "logs"

"""Process-local wiring for the CLI. Agents still do not own state."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from mobyai.agents.orchestrator import OrchestratorAgent
from mobyai.agents.planner import PlannerAgent
from mobyai.agents.worker import WorkerAgent
from mobyai.console.settings import OverlayWatcher
from mobyai.execution.workspace import Workspace
from mobyai.inference.factory import InferenceRegistry
from mobyai.inference.llamacpp import apply_mobyai_env
from mobyai.inference.service_manager import (
    InferenceServiceManager,
    device_for_role,
    service_manager_from_config,
)
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.config import AgentRole, AppConfig, overlay_path_for
from mobyai.models.project import Project
from mobyai.state.repository import StateStore
from mobyai.state.sqlite import SqliteStateStore

RuntimeFactory = Callable[[AppConfig], "Runtime"]


class Runtime:
    """Config + store + agents + service manager. Safe to inject fakes in tests."""

    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        planner: PlannerAgent,
        orchestrator: OrchestratorAgent,
        worker: WorkerAgent,
        services: InferenceServiceManager,
        *,
        owns_store: bool = True,
    ) -> None:
        self.config = config
        self.store = store
        self.planner = planner
        self.orchestrator = orchestrator
        self.worker = worker
        self.services = services
        self._owns_store = owns_store

    @classmethod
    def from_config(cls, config: AppConfig) -> Runtime:
        apply_mobyai_env()
        store = SqliteStateStore(config.persistence.database_path)
        registry = InferenceRegistry.from_config(config)
        repairs = config.runtime.max_schema_repair_attempts
        overlay: Path | None
        try:
            overlay = overlay_path_for(find_config_path())
        except FileNotFoundError:
            overlay = None
        watcher = OverlayWatcher(overlay, config)

        def refresh() -> None:
            watcher.maybe_reload()

        from mobyai.agents.prompts.settings import PromptSnapshot
        prompts = PromptSnapshot(config, refresh)
        runtime = cls(
            config,
            store,
            PlannerAgent(
                StructuredGenerator(
                    registry.get(AgentRole.PLANNER),
                    max_repair_attempts=repairs,
                    settings=config.models.planner,
                    refresh=refresh,
                ),
                max_inspect_turns=config.runtime.max_planner_inspect_turns,
            ),
            OrchestratorAgent(
                StructuredGenerator(
                    registry.get(AgentRole.ORCHESTRATOR),
                    max_repair_attempts=repairs,
                    settings=config.models.orchestrator,
                    recovery_enabled=False,
                    refresh=refresh,
                )
            ),
            WorkerAgent(
                StructuredGenerator(
                    registry.get(AgentRole.WORKER),
                    max_repair_attempts=repairs,
                    settings=config.models.worker,
                    refresh=refresh,
                )
            ),
            service_manager_from_config(config),
        )

        for agent in (runtime.planner, runtime.orchestrator, runtime.worker):
            agent.prompts = prompts
        runtime.prompts = prompts
        return runtime

    async def initialize(self) -> None:
        from mobyai.tracing import configure
        configure(self.config.tracing, self.config.persistence.database_path)
        init = getattr(self.store, "initialize", None)
        if init is not None:
            await init()

    async def aclose(self) -> None:
        from mobyai.tracing import flush
        flush()
        closer = getattr(self.services, "aclose", None)
        if closer is not None:
            await closer()
        if self._owns_store:
            close = getattr(self.store, "close", None)
            if close is not None:
                await close()

    def workspace_for(self, project: Project) -> Workspace:
        extra = self.config.project.extra_allowed_paths
        workspace = Workspace(project.workspace_root, extra_allowed=extra)
        workspace.ensure_root()
        return workspace

    def device_for(self, role: AgentRole) -> int | str | None:
        return device_for_role(self.config, role)

    def gpu_mode_for(self, role: AgentRole) -> str:
        device = self.device_for(role)
        gpu_mode = getattr(self.services, "gpu_mode", None)
        if gpu_mode is None or device is None:
            return "idle"
        return str(gpu_mode(device))


def default_runtime_factory(config: AppConfig) -> Runtime:
    return Runtime.from_config(config)


def find_config_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get("MOBYAI_CONFIG")
    if env:
        return Path(env).expanduser()
    candidates = [
        Path.cwd() / "config.yaml",
        Path.cwd() / "config" / "default.yaml",
        Path.home() / "MobyAI" / "config.yaml",
    ]
    here = Path(__file__).resolve()
    for parent in here.parents:
        bundled = parent / "config" / "default.yaml"
        if bundled.is_file():
            candidates.append(bundled)
            break
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        "No configuration file found. Pass --config or run mobyai init."
    )


def bundled_default_config() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "default.yaml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Bundled config/default.yaml is missing")

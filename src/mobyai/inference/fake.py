"""In-process provider for tests. Does not speak HTTP or require a GPU."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from mobyai.inference.service_manager import RoleUnhealthyError
from mobyai.models.config import AgentRole
from mobyai.models.inference import (
    GpuServiceMode,
    InferenceEndpoint,
    ModelRequest,
    ModelResponse,
)


class FakeInferenceProvider:
    def __init__(self, responses: Sequence[str] | Iterable[str]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []
        self.healthy = True

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise IndexError("FakeInferenceProvider has no remaining responses")
        content = self._responses.pop(0)
        return ModelResponse(content=content, model="fake", finish_reason="stop")

    async def healthcheck(self) -> bool:
        return self.healthy


class FakeInferenceServiceManager:
    """Records ensure/stop calls. Does not start processes or talk to GPUs."""

    def __init__(self) -> None:
        self.ensured: list[AgentRole] = []
        self.stopped: list[AgentRole] = []
        self.fail_roles: set[AgentRole] = set()
        self.healthy: dict[AgentRole, bool] = {role: True for role in AgentRole}

    async def ensure_role_available(self, role: AgentRole) -> InferenceEndpoint:
        self.ensured.append(role)
        if role in self.fail_roles:
            raise RoleUnhealthyError(f"fake {role.value} is unhealthy")
        return InferenceEndpoint(
            role=role,
            base_url=f"http://127.0.0.1/{role.value}",
            model="fake",
            managed=False,
            gpu_mode={
                AgentRole.PLANNER: GpuServiceMode.PLANNER_MODE,
                AgentRole.WORKER: GpuServiceMode.WORKER_MODE,
                AgentRole.ORCHESTRATOR: GpuServiceMode.ORCHESTRATOR_MODE,
            }[role],
        )

    async def healthcheck(self, role: AgentRole) -> bool:
        return self.healthy.get(role, True)

    async def stop_role(self, role: AgentRole) -> None:
        self.stopped.append(role)


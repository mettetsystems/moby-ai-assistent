"""Worker agent: propose structured tool calls. It does not write files or own tests."""

from __future__ import annotations

from collections.abc import Sequence

from mobyai.agents.prompts.worker import SYSTEM_PROMPT, USER_PROMPT
from mobyai.context.types import WorkerContext
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.inference import ChatMessage, ModelRequest
from mobyai.models.worker import WorkerResult


class WorkerAgent:
    def __init__(self, generator: StructuredGenerator) -> None:
        self._generator = generator
        self.prompts = None

    @property
    def refresh_context_enabled(self):
        return bool(self._generator.settings and self._generator.settings.refresh_worker_context)

    @property
    def compact_history_enabled(self):
        return bool(self._generator.settings and self._generator.settings.compact_worker_history)

    async def implement(
        self,
        context: WorkerContext,
        *,
        extra_messages: Sequence[ChatMessage] = (),
    ) -> WorkerResult:
        from mobyai.execution.tool_contracts import tool_guidance
        from mobyai.sandbox.policy import guidance
        request = ModelRequest(
            messages=[
                ChatMessage(role="system", content=(self.prompts.get("worker") if self.prompts else SYSTEM_PROMPT) + "\n" + tool_guidance() + "\n" + guidance()),
                ChatMessage(role="user", content=USER_PROMPT.format(context=context.render())),
                *extra_messages,
            ]
        )
        return await self._generator.generate(request, WorkerResult)

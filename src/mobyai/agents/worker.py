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

    async def implement(
        self,
        context: WorkerContext,
        *,
        extra_messages: Sequence[ChatMessage] = (),
    ) -> WorkerResult:
        request = ModelRequest(
            messages=[
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=USER_PROMPT.format(context=context.render())),
                *extra_messages,
            ]
        )
        return await self._generator.generate(request, WorkerResult)

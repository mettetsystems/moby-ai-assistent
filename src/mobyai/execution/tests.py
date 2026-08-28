"""Test execution. Pass/fail is the process result, not a model claim."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mobyai.execution.commands import CommandResult, CommandRunner
from mobyai.execution.workspace import Workspace


class TestRunner:
    __test__ = False

    def __init__(self, runner: CommandRunner, workspace: Workspace) -> None:
        self.runner = runner
        self.workspace = workspace

    async def run(
        self,
        command: list[str],
        *,
        timeout_seconds: int = 600,
        cwd: Path | str | None = None,
    ) -> CommandResult:
        return await self.runner.run(
            command,
            cwd=cwd or self.workspace.root,
            timeout_seconds=timeout_seconds,
        )

    async def run_all(
        self,
        commands: Sequence[list[str]],
        *,
        timeout_seconds: int = 600,
    ) -> list[CommandResult]:
        results: list[CommandResult] = []
        for command in commands:
            result = await self.run(command, timeout_seconds=timeout_seconds)
            results.append(result)
            if not passed(result):
                break
        return results


def passed(result: CommandResult) -> bool:
    return result.exit_code == 0 and not result.timed_out

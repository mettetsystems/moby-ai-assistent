"""Persist full command logs on disk. Models receive a reduced view separately."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mobyai.execution.commands import CommandResult
from mobyai.execution.workspace import Workspace


class AttemptLogStore:
    def __init__(self, workspace: Workspace, *, max_log_bytes: int = 52_428_800) -> None:
        self.workspace = workspace
        self.max_log_bytes = max_log_bytes

    def path_for(self, task_id: str, attempt: int) -> Path:
        safe_task = task_id.replace("/", "_").replace("..", "_")
        return self.workspace.root / "artifacts" / "logs" / safe_task / f"attempt_{attempt:03d}.log"

    async def persist(
        self,
        task_id: str,
        attempt: int,
        result: CommandResult,
    ) -> Path:
        path = self.workspace.resolve(self.path_for(task_id, attempt))
        text = format_command_log(result)
        if len(text.encode("utf-8")) > self.max_log_bytes:
            encoded = text.encode("utf-8")[: self.max_log_bytes]
            text = encoded.decode("utf-8", errors="replace") + "\n...[log truncated]...\n"
        await asyncio.to_thread(_write, path, text)
        return path


_STDOUT_MARK = "\n===== stdout =====\n"
_STDERR_MARK = "\n===== stderr =====\n"


def format_command_log(result: CommandResult) -> str:
    command = " ".join(result.command)
    return (
        f"command: {command}\n"
        f"exit_code: {result.exit_code}\n"
        f"timed_out: {result.timed_out}\n"
        f"duration_seconds: {result.duration_seconds}\n"
        f"stdout_truncated: {result.stdout_truncated}\n"
        f"stderr_truncated: {result.stderr_truncated}\n"
        f"{_STDOUT_MARK}"
        f"{result.stdout}"
        f"{_STDERR_MARK}"
        f"{result.stderr}\n"
    )


def parse_command_log(text: str) -> CommandResult:
    """Rebuild a CommandResult from ``format_command_log`` output."""
    meta: dict[str, str] = {}
    stdout = ""
    stderr = ""
    if _STDOUT_MARK in text and _STDERR_MARK in text:
        header, rest = text.split(_STDOUT_MARK, 1)
        stdout, stderr = rest.split(_STDERR_MARK, 1)
        if stderr.endswith("\n"):
            stderr = stderr[:-1]
        for line in header.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                meta[key] = value
    else:
        stdout = text
    command = meta.get("command", "").split() or ["unknown"]
    return CommandResult(
        command=command,
        exit_code=int(meta.get("exit_code", "1")),
        stdout=stdout,
        stderr=stderr,
        duration_seconds=float(meta.get("duration_seconds", "0") or 0),
        timed_out=meta.get("timed_out", "False") == "True",
        stdout_truncated=meta.get("stdout_truncated", "False") == "True",
        stderr_truncated=meta.get("stderr_truncated", "False") == "True",
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

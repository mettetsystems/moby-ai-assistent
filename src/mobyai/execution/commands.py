"""Process execution with timeouts, output caps, and a destructive-command gate."""

from __future__ import annotations

from mobyai.tracing import traced

import asyncio
import os
import signal
import shutil
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mobyai.execution.errors import DestructiveCommandError, PathEscapeError, WorkspaceError
from mobyai.execution.workspace import Workspace

_DESTRUCTIVE_PROGRAMS = frozenset(
    {
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "dd",
        "mkfs",
        "mkfs.ext4",
        "mkfs.xfs",
        "mkfs.btrfs",
        "wipefs",
        "fdisk",
        "parted",
        "reboot",
        "shutdown",
        "halt",
        "poweroff",
        "sudo",
    }
)


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class CommandRunner:
    def __init__(
        self,
        workspace: Workspace,
        *,
        max_output_bytes: int = 1_048_576,
        allow_destructive: bool = False,
    ) -> None:
        self.workspace = workspace
        self.max_output_bytes = max_output_bytes
        self.allow_destructive = allow_destructive

    @traced("command.run")
    async def run(
        self,
        command: list[str],
        *,
        cwd: Path | str | None = None,
        timeout_seconds: int = 600,
    ) -> CommandResult:
        if not command:
            raise WorkspaceError("Command must be a non-empty argv list")
        if any(not isinstance(part, str) or part == "" for part in command):
            raise WorkspaceError("Command argv must be non-empty strings")
        if not self.allow_destructive and _is_destructive(command):
            raise DestructiveCommandError(f"Destructive command denied: {command[0]}")

        workdir = self.workspace.resolve(cwd) if cwd is not None else self.workspace.root
        if not workdir.is_dir():
            raise WorkspaceError(f"cwd is not a directory: {workdir}")
        if not self.workspace.contains(workdir):
            raise PathEscapeError(f"cwd escapes workspace: {cwd}")

        # Match npm's local-tool lookup for saved bare test commands, including
        # commands launched in a nested project directory. Never leave the workspace.
        executable = command[0]
        if "/" not in executable:
            directory = workdir
            while self.workspace.contains(directory):
                if (directory / "package.json").is_file():
                    local = shutil.which(executable, path=str(directory / "node_modules" / ".bin"))
                    if local:
                        executable = local
                        break
                if directory == self.workspace.root or directory.parent == directory:
                    break
                directory = directory.parent

        stdout_buf = _BoundedBuffer(self.max_output_bytes)
        stderr_buf = _BoundedBuffer(self.max_output_bytes)
        started = time.monotonic()
        timed_out = False
        try:
            process = await asyncio.create_subprocess_exec(
                executable, *command[1:],
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            return CommandResult(command=list(command), exit_code=127, stdout="",
                stderr=f"Executable not found: {command[0]}. Install or configure the required toolchain.",
                duration_seconds=round(time.monotonic() - started, 4))
        try:
            await asyncio.wait_for(
                _drain(process, stdout_buf, stderr_buf),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            _kill_process_group(process.pid)
            await process.wait()
            raise
        except TimeoutError:
            timed_out = True
            _kill_process_group(process.pid)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
            await _finish_drain(process, stdout_buf, stderr_buf)

        duration = time.monotonic() - started
        exit_code = process.returncode
        if exit_code is None:
            exit_code = -1
        return CommandResult(
            command=list(command),
            exit_code=exit_code if not timed_out else -1,
            stdout=stdout_buf.text(),
            stderr=stderr_buf.text(),
            duration_seconds=round(duration, 4),
            timed_out=timed_out,
            stdout_truncated=stdout_buf.truncated,
            stderr_truncated=stderr_buf.truncated,
        )


def _is_destructive(command: list[str]) -> bool:
    program = Path(command[0]).name
    if program in _DESTRUCTIVE_PROGRAMS:
        return True
    if program == "find" and "-delete" in command:
        return True
    if program == "git" and len(command) > 1 and command[1] == "clean":
        return True
    return False


def _kill_process_group(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class _BoundedBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = max(limit, 1)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        remaining = self.limit - len(self.head)
        if remaining > 0:
            self.head.extend(chunk[:remaining])
        self.tail.extend(chunk)
        overflow = len(self.tail) - self.limit
        if overflow > 0:
            del self.tail[:overflow]

    def text(self) -> str:
        if not self.truncated:
            return _decode(bytes(self.head))
        return (
            _decode(bytes(self.head))
            + "\n...[truncated]...\n"
            + _decode(bytes(self.tail))
        )


async def _drain(
    process: asyncio.subprocess.Process,
    stdout_buf: _BoundedBuffer,
    stderr_buf: _BoundedBuffer,
) -> None:
    assert process.stdout is not None
    assert process.stderr is not None
    await asyncio.gather(
        _read_stream(process.stdout, stdout_buf),
        _read_stream(process.stderr, stderr_buf),
        process.wait(),
    )


async def _finish_drain(
    process: asyncio.subprocess.Process,
    stdout_buf: _BoundedBuffer,
    stderr_buf: _BoundedBuffer,
) -> None:
    tasks = []
    if process.stdout is not None:
        tasks.append(_read_stream(process.stdout, stdout_buf))
    if process.stderr is not None:
        tasks.append(_read_stream(process.stderr, stderr_buf))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _read_stream(stream: asyncio.StreamReader, buffer: _BoundedBuffer) -> None:
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return
        buffer.feed(chunk)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")

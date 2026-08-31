"""Dispatch Worker tool calls through the workspace jail. The model never writes files."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mobyai.context.tokenizer import truncate_to_tokens
from mobyai.execution.commands import CommandResult, CommandRunner
from mobyai.execution.errors import WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.patches import apply_patch
from mobyai.execution.tests import TestRunner
from mobyai.execution.workspace import Workspace
from mobyai.models.worker import WorkerToolCall, WorkerToolName

_OUTPUT_TOKEN_BUDGET = 4000
_RANGE_HINT = (
    "\n# truncated at ~4000 tokens; use start_line and end_line (or limit) to read a range"
)

PLANNER_INSPECT_TOOLS: frozenset[WorkerToolName] = frozenset(
    {
        WorkerToolName.READ_FILE,
        WorkerToolName.LIST_DIRECTORY,
        WorkerToolName.SEARCH_CODE,
        WorkerToolName.GLOB,
        WorkerToolName.FIND_FILES,
    }
)


class ToolOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkerToolName
    ok: bool
    output: str = ""
    error: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False


class ToolDispatcher:
    def __init__(
        self,
        filesystem: WorkspaceFilesystem,
        workspace: Workspace,
        commands: CommandRunner,
        tests: TestRunner,
        *,
        timeout_seconds: int = 600,
    ) -> None:
        self._filesystem = filesystem
        self._workspace = workspace
        self._commands = commands
        self._tests = tests
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_workspace(
        cls, workspace: Workspace, *, timeout_seconds: int = 600
    ) -> ToolDispatcher:
        filesystem = WorkspaceFilesystem(workspace)
        commands = CommandRunner(workspace)
        tests = TestRunner(commands, workspace)
        return cls(
            filesystem, workspace, commands, tests, timeout_seconds=timeout_seconds
        )

    async def execute_all(
        self,
        calls: Sequence[WorkerToolCall],
        *,
        allowed: frozenset[WorkerToolName] | None = None,
    ) -> list[ToolOutcome]:
        return [await self.execute(call, allowed=allowed) for call in calls]

    async def execute(
        self,
        call: WorkerToolCall,
        *,
        allowed: frozenset[WorkerToolName] | None = None,
    ) -> ToolOutcome:
        if allowed is not None and call.name not in allowed:
            return ToolOutcome(
                name=call.name,
                ok=False,
                error=f"{call.name.value} is not allowed for this agent",
            )
        try:
            return await self._dispatch(call.name, call.arguments)
        except WorkspaceError as exc:
            return ToolOutcome(name=call.name, ok=False, error=str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            return ToolOutcome(
                name=call.name,
                ok=False,
                error=f"Invalid arguments: {exc}",
            )

    async def _dispatch(
        self, name: WorkerToolName, arguments: Mapping[str, Any]
    ) -> ToolOutcome:
        if name is WorkerToolName.READ_FILE:
            path = _require_str(arguments, "path")
            content = await self._filesystem.read_file(path)
            return ToolOutcome(name=name, ok=True, output=_read_output(content, arguments))
        if name is WorkerToolName.WRITE_FILE:
            path = _require_str(arguments, "path")
            contents = _require_str(arguments, "contents", allow_empty=True)
            written = await self._filesystem.write_file(path, contents)
            return ToolOutcome(name=name, ok=True, output=written, changed_files=[written])
        if name is WorkerToolName.APPLY_PATCH:
            diff = _patch_text(arguments)
            changed = apply_patch(self._workspace, diff)
            return ToolOutcome(
                name=name,
                ok=True,
                output="\n".join(changed),
                changed_files=list(changed),
            )
        if name is WorkerToolName.LIST_DIRECTORY:
            path = _optional_str(arguments, "path") or "."
            entries = await self._filesystem.list_directory(path)
            return ToolOutcome(name=name, ok=True, output="\n".join(entries))
        if name is WorkerToolName.SEARCH_CODE:
            pattern = _require_str(arguments, "pattern")
            path = _optional_str(arguments, "path") or "."
            max_hits = _optional_int(arguments, "max_hits") or 50
            hits = await self._filesystem.search_code(pattern, path, max_hits=max_hits)
            lines = [f"{hit.path}:{hit.line_number}:{hit.line}" for hit in hits]
            return ToolOutcome(name=name, ok=True, output=_trim("\n".join(lines)))
        if name in {WorkerToolName.GLOB, WorkerToolName.FIND_FILES}:
            pattern = _require_str(arguments, "pattern")
            path = _optional_str(arguments, "path") or "."
            max_hits = _optional_int(arguments, "max_hits") or 100
            matches = await self._filesystem.glob_files(pattern, path, max_hits=max_hits)
            return ToolOutcome(name=name, ok=True, output="\n".join(matches))
        if name is WorkerToolName.RUN_TEST:
            command = split_command(arguments.get("command"))
            result = await self._tests.run(command, timeout_seconds=self._timeout_seconds)
            return _from_command(name, result)
        if name is WorkerToolName.RUN_COMMAND:
            command = split_command(arguments.get("command"))
            cwd = _optional_str(arguments, "cwd")
            result = await self._commands.run(
                command,
                cwd=cwd,
                timeout_seconds=self._timeout_seconds,
            )
            return _from_command(name, result)
        raise ValueError(f"Unsupported tool: {name}")


def split_command(value: object) -> list[str]:
    if isinstance(value, list):
        parts = [item for item in value if isinstance(item, str) and item != ""]
        if len(parts) != len(value) or not parts:
            raise ValueError("command must be a non-empty argv list of strings")
        return parts
    if isinstance(value, str) and value.strip():
        parts = shlex.split(value)
        if not parts:
            raise ValueError("command is empty")
        return parts
    raise ValueError("command must be an argv list or a non-empty string")


def format_tool_outcomes(outcomes: Sequence[ToolOutcome]) -> str:
    payload = [item.model_dump(mode="json") for item in outcomes]
    return json.dumps(payload, indent=2)


def _from_command(name: WorkerToolName, result: CommandResult) -> ToolOutcome:
    body = (
        f"exit_code: {result.exit_code}\n"
        f"timed_out: {result.timed_out}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return ToolOutcome(
        name=name,
        ok=result.exit_code == 0 and not result.timed_out,
        output=_trim(body),
        exit_code=result.exit_code,
        timed_out=result.timed_out,
    )


def _require_str(
    arguments: Mapping[str, Any], key: str, *, allow_empty: bool = False
) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if not allow_empty and value.strip() == "":
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(arguments: Mapping[str, Any], key: str) -> str | None:
    if key not in arguments or arguments[key] is None:
        return None
    return _require_str(arguments, key)


def _optional_int(arguments: Mapping[str, Any], key: str) -> int | None:
    if key not in arguments or arguments[key] is None:
        return None
    value = arguments[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _patch_text(arguments: Mapping[str, Any]) -> str:
    for key in ("diff", "patch"):
        if key in arguments:
            return _require_str(arguments, key)
    raise ValueError("apply_patch requires diff")


def _read_output(content: str, arguments: Mapping[str, Any]) -> str:
    start_line = _optional_int(arguments, "start_line")
    if start_line is None:
        start_line = _optional_int(arguments, "offset")
    end_line = _optional_int(arguments, "end_line")
    limit = _optional_int(arguments, "limit")
    ranged = start_line is not None or end_line is not None or limit is not None
    if ranged:
        body, header = _slice_lines(
            content, start_line=start_line, end_line=end_line, limit=limit
        )
        payload = f"{header}\n{body}"
    else:
        payload = content
    trimmed, truncated = truncate_to_tokens(payload, _OUTPUT_TOKEN_BUDGET)
    if truncated:
        return trimmed + _RANGE_HINT
    return trimmed


def _slice_lines(
    text: str,
    *,
    start_line: int | None,
    end_line: int | None,
    limit: int | None,
) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = 1 if start_line is None else start_line
    if start < 1:
        raise ValueError("start_line must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    if end_line is None:
        end = start + limit - 1 if limit is not None else max(total, start)
    else:
        if end_line < 1:
            raise ValueError("end_line must be >= 1")
        end = end_line
        if limit is not None:
            end = min(end, start + limit - 1)
    if end < start:
        raise ValueError("end_line must be >= start_line")
    if total == 0 or start > total:
        return "", f"# lines {start}-{end} of {total} (empty: start_line past end of file)"
    actual_end = min(end, total)
    header = f"# lines {start}-{actual_end} of {total}"
    if start > 1 or actual_end < total:
        header += "; pass start_line/end_line to read another range"
    return "".join(lines[start - 1 : actual_end]), header


def _trim(text: str) -> str:
    trimmed, _truncated = truncate_to_tokens(text, _OUTPUT_TOKEN_BUDGET)
    return trimmed

"""Reduce command logs for model prompts. Full logs stay on disk."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from mobyai.execution.commands import CommandResult

_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):.*?(?:\n[A-Za-z_][\w.]*Error[^\n]*)",
    re.DOTALL,
)
_FAILED_TEST_RE = re.compile(r"^(?:FAILED|ERROR) ([^\s]+)", re.MULTILINE)
_ASSERT_RE = re.compile(r"^E\s+.*$", re.MULTILINE)


class ReducedLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: list[str]
    exit_code: int
    timed_out: bool
    stdout_head: str = ""
    stdout_tail: str = ""
    stderr_head: str = ""
    stderr_tail: str = ""
    traceback: str | None = None
    failed_tests: list[str] = Field(default_factory=list)
    assertion_differences: list[str] = Field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"command: {' '.join(self.command)}",
            f"exit_code: {self.exit_code}",
            f"timed_out: {self.timed_out}",
        ]
        if self.failed_tests:
            lines.append("failed_tests:")
            lines.extend(f"- {name}" for name in self.failed_tests)
        if self.assertion_differences:
            lines.append("assertions:")
            lines.extend(self.assertion_differences)
        if self.traceback:
            lines.append("traceback:")
            lines.append(self.traceback)
        for label, head, tail in (
            ("stdout", self.stdout_head, self.stdout_tail),
            ("stderr", self.stderr_head, self.stderr_tail),
        ):
            if head and not tail:
                lines.append(f"{label}:")
                lines.append(head)
            elif head or tail:
                lines.append(f"{label}_head:")
                lines.append(head)
                lines.append(f"{label}_tail:")
                lines.append(tail)
        return "\n".join(lines)


class LogReducer:
    def __init__(self, tail_lines: int = 150) -> None:
        self.tail_lines = max(tail_lines, 1)

    def reduce(self, result: CommandResult) -> ReducedLog:
        stdout_head, stdout_tail = _head_tail(result.stdout, self.tail_lines)
        stderr_head, stderr_tail = _head_tail(result.stderr, self.tail_lines)
        combined = "\n".join(
            part for part in (result.stdout, result.stderr) if part
        )
        traceback = _last_match(_TRACEBACK_RE, combined)
        failed = _FAILED_TEST_RE.findall(combined)
        assertions = [line.strip() for line in _ASSERT_RE.findall(combined)]
        return ReducedLog(
            command=list(result.command),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout_head=stdout_head,
            stdout_tail=stdout_tail,
            stderr_head=stderr_head,
            stderr_tail=stderr_tail,
            traceback=traceback,
            failed_tests=_unique(failed),
            assertion_differences=_unique(assertions),
        )

    def reduce_log_text(self, text: str) -> ReducedLog:
        from mobyai.execution.logs import parse_command_log

        return self.reduce(parse_command_log(text))


def _head_tail(text: str, line_count: int) -> tuple[str, str]:
    if not text:
        return "", ""
    lines = text.splitlines()
    if len(lines) <= line_count:
        return text, ""
    head = "\n".join(lines[:line_count])
    tail = "\n".join(lines[-line_count:])
    return head, tail


def _last_match(pattern: re.Pattern[str], text: str) -> str | None:
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    return matches[-1].group(0).strip()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered

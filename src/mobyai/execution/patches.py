"""Apply unified diffs inside a workspace. The model never writes files directly."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mobyai.execution.errors import PatchError
from mobyai.execution.workspace import Workspace


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


@dataclass
class _FilePatch:
    old_path: str
    new_path: str
    hunks: list[_Hunk] = field(default_factory=list)


def apply_patch(workspace: Workspace, diff_text: str) -> list[str]:
    """Apply a unified diff. Returns workspace-relative paths that changed."""
    patches = parse_unified_diff(diff_text)
    if not patches:
        raise PatchError("Patch contained no file hunks")
    changed: list[str] = []
    for file_patch in patches:
        changed.append(_apply_file_patch(workspace, file_patch))
    return changed


def parse_unified_diff(diff_text: str) -> list[_FilePatch]:
    lines = diff_text.splitlines()
    patches: list[_FilePatch] = []
    current: _FilePatch | None = None
    hunk: _Hunk | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            if current is not None:
                if hunk is not None:
                    current.hunks.append(hunk)
                    hunk = None
                patches.append(current)
            old_path = _parse_path(line[4:])
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise PatchError("Expected +++ path after --- path")
            new_path = _parse_path(lines[index + 1][4:])
            current = _FilePatch(old_path=old_path, new_path=new_path)
            index += 2
            continue
        if line.startswith("@@ "):
            if current is None:
                raise PatchError("Hunk without a file header")
            if hunk is not None:
                current.hunks.append(hunk)
            hunk = _parse_hunk_header(line)
            index += 1
            continue
        if hunk is not None and (line.startswith(" ") or line.startswith("+") or line.startswith("-")):
            hunk.lines.append(line)
            index += 1
            continue
        if line.startswith("\\"):
            index += 1
            continue
        index += 1
    if current is not None:
        if hunk is not None:
            current.hunks.append(hunk)
        patches.append(current)
    return patches


def _apply_file_patch(workspace: Workspace, file_patch: _FilePatch) -> str:
    creating = file_patch.old_path == "/dev/null"
    deleting = file_patch.new_path == "/dev/null"
    target_label = file_patch.old_path if deleting else file_patch.new_path
    if target_label == "/dev/null":
        raise PatchError("Patch does not name a workspace file")
    target = workspace.resolve(target_label)
    relative = workspace.relative(target)

    if deleting:
        if not target.exists():
            raise PatchError(f"Cannot delete missing file: {relative}")
        if target.is_dir():
            raise PatchError(f"Refusing to delete directory via patch: {relative}")
        target.unlink()
        return relative

    if creating:
        if target.exists():
            raise PatchError(f"Cannot create file that already exists: {relative}")
        new_lines = _lines_from_hunks(file_patch.hunks, expected_prefix="+")
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_lines(target, new_lines)
        return relative

    if not target.is_file():
        raise PatchError(f"Cannot patch missing file: {relative}")
    original = target.read_text(encoding="utf-8")
    updated = _apply_hunks(original, file_patch.hunks)
    _write_lines(target, updated.splitlines())
    return relative


def _apply_hunks(original: str, hunks: list[_Hunk]) -> str:
    source = original.splitlines()
    output: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = hunk.old_start - 1
        if start < cursor:
            raise PatchError("Hunks overlap or are out of order")
        output.extend(source[cursor:start])
        hunk_old = [line[1:] for line in hunk.lines if line.startswith("-") or line.startswith(" ")]
        hunk_new = [line[1:] for line in hunk.lines if line.startswith("+") or line.startswith(" ")]
        actual = source[start : start + len(hunk_old)]
        if actual != hunk_old:
            raise PatchError("Hunk context does not match file contents")
        output.extend(hunk_new)
        cursor = start + len(hunk_old)
    output.extend(source[cursor:])
    return "\n".join(output) + ("\n" if original.endswith("\n") else "")


def _lines_from_hunks(hunks: list[_Hunk], expected_prefix: str) -> list[str]:
    lines: list[str] = []
    for hunk in hunks:
        for line in hunk.lines:
            if line.startswith(expected_prefix) or line.startswith(" "):
                lines.append(line[1:])
            elif line.startswith("-") and expected_prefix == "+":
                continue
            else:
                raise PatchError("Create/delete hunk has unexpected line prefix")
    return lines


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _parse_path(raw: str) -> str:
    token = raw.strip()
    if token.startswith("/dev/null"):
        return "/dev/null"
    if "\t" in token:
        token = token.split("\t", 1)[0]
    if token.startswith("a/") or token.startswith("b/"):
        token = token[2:]
    if token.startswith("/") or token.startswith("..") or ".." in Path(token).parts:
        raise PatchError(f"Patch path is not workspace-relative: {token}")
    if token == "":
        raise PatchError("Patch path is empty")
    return token


def _parse_hunk_header(line: str) -> _Hunk:
    # @@ -l,s +l,s @@ optional
    try:
        body = line.split("@@")[1].strip()
        old_part, new_part = body.split(" ")[0], body.split(" ")[1]
        old_start, old_count = _parse_range(old_part[1:])
        new_start, new_count = _parse_range(new_part[1:])
    except (IndexError, ValueError) as exc:
        raise PatchError(f"Invalid hunk header: {line}") from exc
    return _Hunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        lines=[],
    )


def _parse_range(text: str) -> tuple[int, int]:
    if "," in text:
        start, count = text.split(",", 1)
        return int(start), int(count)
    return int(text), 1

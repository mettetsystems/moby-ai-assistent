"""Safe file operations inside a workspace."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from mobyai.execution.errors import PathEscapeError, WorkspaceError
from mobyai.execution.workspace import Workspace

_SKIP_DIR_NAMES = frozenset(
    {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache"}
)


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    line_number: int
    line: str


class WorkspaceFilesystem:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def read_file(self, path: str) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        return await asyncio.to_thread(target.read_text, encoding="utf-8")

    async def write_file(self, path: str, contents: str) -> str:
        target = self.workspace.resolve(path)
        if target.exists() and target.is_dir():
            raise WorkspaceError(f"Cannot write to directory: {path}")
        parent = target.parent
        if not self.workspace.contains(parent.resolve()):
            raise PathEscapeError(f"Path escapes workspace: {path}")
        await asyncio.to_thread(_atomic_write, target, contents)
        return self.workspace.relative(target)

    async def list_directory(self, path: str = ".") -> list[str]:
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")

        def _list() -> list[str]:
            names: list[str] = []
            for child in sorted(target.iterdir(), key=lambda item: item.name):
                rel = self.workspace.relative(child)
                if child.is_dir() and not child.is_symlink():
                    names.append(rel + "/")
                else:
                    names.append(rel)
            return names

        return await asyncio.to_thread(_list)

    async def search_code(
        self,
        pattern: str,
        path: str = ".",
        *,
        max_hits: int = 50,
    ) -> list[SearchHit]:
        root = self.workspace.resolve(path, must_exist=True)
        compiled = re.compile(pattern)

        def _search() -> list[SearchHit]:
            hits: list[SearchHit] = []
            files = [root] if root.is_file() else _iter_files(root)
            for file_path in files:
                if not self.workspace.contains(file_path.resolve()):
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if "\0" in text:
                    continue
                rel = self.workspace.relative(file_path)
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if compiled.search(line):
                        hits.append(
                            SearchHit(path=rel, line_number=line_number, line=line)
                        )
                        if len(hits) >= max_hits:
                            return hits
            return hits

        return await asyncio.to_thread(_search)

    async def glob_files(
        self,
        pattern: str,
        path: str = ".",
        *,
        max_hits: int = 100,
    ) -> list[str]:
        if not pattern or not str(pattern).strip():
            raise WorkspaceError("glob pattern must be non-empty")
        compiled = str(pattern).strip()
        probe = Path(compiled)
        if probe.is_absolute() or ".." in probe.parts:
            raise PathEscapeError(f"Path escapes workspace: {pattern}")
        root = self.workspace.resolve(path, must_exist=True)
        if not root.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")
        limit = max(max_hits, 1)

        def _glob() -> list[str]:
            matches: list[str] = []
            try:
                found = root.glob(compiled)
            except ValueError as exc:
                raise WorkspaceError(f"Invalid glob pattern: {pattern}") from exc
            for item in sorted(found, key=lambda candidate: candidate.as_posix()):
                if any(part in _SKIP_DIR_NAMES for part in item.parts):
                    continue
                try:
                    resolved = item.resolve()
                except OSError:
                    continue
                if not resolved.is_file() or not self.workspace.contains(resolved):
                    continue
                matches.append(self.workspace.relative(resolved))
                if len(matches) >= limit:
                    break
            return matches

        return await asyncio.to_thread(_glob)

    async def list_tree(self, path: str = ".", *, max_entries: int = 200) -> list[str]:
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")

        def _walk() -> list[str]:
            entries: list[str] = []
            root = self.workspace.root
            for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
                dirnames[:] = sorted(
                    name for name in dirnames if name not in _SKIP_DIR_NAMES
                )
                current = Path(dirpath)
                relative_dir = current.relative_to(root).as_posix()
                if relative_dir != ".":
                    entries.append(relative_dir + "/")
                    if len(entries) >= max_entries:
                        entries.append("...[truncated]...")
                        return entries
                for filename in sorted(filenames):
                    rel = (current / filename).relative_to(root).as_posix()
                    entries.append(rel)
                    if len(entries) >= max_entries:
                        entries.append("...[truncated]...")
                        return entries
            return entries

        return await asyncio.to_thread(_walk)


def _atomic_write(target, contents: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(contents, encoding="utf-8")
        tmp_path.replace(target)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
        for filename in filenames:
            yield Path(dirpath) / filename

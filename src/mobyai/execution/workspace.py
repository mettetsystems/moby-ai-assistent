"""Project-root jail. All file and process paths must resolve inside it."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mobyai.execution.errors import PathEscapeError, WorkspaceError


class Workspace:
    def __init__(
        self,
        root: Path | str,
        extra_allowed: Sequence[Path | str] = (),
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.extra_allowed = tuple(
            Path(path).expanduser().resolve() for path in extra_allowed
        )

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def resolve(self, user_path: str | Path, *, must_exist: bool = False) -> Path:
        raw = Path(str(user_path)).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise WorkspaceError(f"Path not found or dangling symlink: {user_path}") from exc
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"Cannot resolve path (possibly a symlink loop): {user_path}: {exc}") from exc
        if not self.contains(resolved):
            raise PathEscapeError(f"Path escapes workspace: {user_path}")
        return resolved

    def contains(self, path: Path | str) -> bool:
        try:
            resolved = Path(path).resolve()
        except (OSError, RuntimeError):
            return False
        return any(_is_under(resolved, root) for root in self.allowed_roots())

    def allowed_roots(self) -> tuple[Path, ...]:
        return (self.root, *self.extra_allowed)

    def relative(self, path: Path | str) -> str:
        resolved = self.resolve(path)
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return resolved.as_posix()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

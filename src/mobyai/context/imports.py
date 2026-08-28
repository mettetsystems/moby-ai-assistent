"""Resolve local Python imports to workspace-relative candidate paths."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def candidate_import_paths(source: str, current_file: str) -> list[str]:
    """Return possible project-relative paths imported by ``current_file``.

    Only one hop from the target is considered. Stdlib modules are skipped.
    Candidates that are not present on disk are filtered later by the packer.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    package_dir = Path(current_file).parent
    found: list[str] = []
    seen: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _add_module_paths(
                    alias.name,
                    level=0,
                    package_dir=package_dir,
                    found=found,
                    seen=seen,
                )
        elif isinstance(node, ast.ImportFrom):
            _add_module_paths(
                node.module or "",
                level=node.level,
                package_dir=package_dir,
                found=found,
                seen=seen,
            )

    return found


def _add_module_paths(
    module: str,
    *,
    level: int,
    package_dir: Path,
    found: list[str],
    seen: set[str],
) -> None:
    if level == 0:
        if not module:
            return
        top = module.split(".", 1)[0]
        if top in sys.stdlib_module_names:
            return
        base = Path(*module.split("."))
    else:
        base = package_dir
        for _ in range(level - 1):
            parent = base.parent
            if parent == base:
                break
            base = parent
        if module:
            base = base / Path(*module.split("."))

    for candidate in (Path(str(base) + ".py"), base / "__init__.py"):
        posix = candidate.as_posix()
        if posix.startswith("../") or posix == ".." or posix.startswith("/"):
            continue
        if posix.startswith("./"):
            posix = posix[2:]
        if posix not in seen:
            seen.add(posix)
            found.append(posix)

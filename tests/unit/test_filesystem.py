from pathlib import Path

import pytest

from mobyai.execution.errors import PathEscapeError, WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.workspace import Workspace


def _workspace(tmp_path: Path) -> tuple[Workspace, WorkspaceFilesystem]:
    root = tmp_path / "project"
    root.mkdir()
    space = Workspace(root)
    return space, WorkspaceFilesystem(space)


def test_relative_resolve(tmp_path: Path) -> None:
    space, _files = _workspace(tmp_path)
    assert space.resolve("src/app.py") == space.root / "src" / "app.py"


async def test_write_and_read_round_trip(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    rel = await files.write_file("src/app.py", "print('hi')\n")
    assert rel == "src/app.py"
    assert await files.read_file("src/app.py") == "print('hi')\n"


async def test_rejects_parent_traversal(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    with pytest.raises(PathEscapeError):
        space.resolve("../secret.txt")
    with pytest.raises(PathEscapeError):
        await files.write_file("../outside.py", "nope\n")


async def test_rejects_absolute_path_outside(tmp_path: Path) -> None:
    space, _files = _workspace(tmp_path)
    with pytest.raises(PathEscapeError):
        space.resolve("/etc/passwd")


async def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    space, files = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = space.root / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(PathEscapeError):
        await files.read_file("link.txt")
    with pytest.raises(PathEscapeError):
        await files.write_file("link.txt", "overwrite\n")


async def test_extra_allowed_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    extra = tmp_path / "shared"
    root.mkdir()
    extra.mkdir()
    (extra / "lib.py").write_text("X = 1\n", encoding="utf-8")
    space = Workspace(root, extra_allowed=[extra])
    files = WorkspaceFilesystem(space)
    assert await files.read_file(extra / "lib.py") == "X = 1\n"


async def test_list_and_search(tmp_path: Path) -> None:
    _space, files = _workspace(tmp_path)
    await files.write_file("src/a.py", "def add(x):\n    return x + 1\n")
    await files.write_file("src/b.py", "def sub(x):\n    return x - 1\n")
    listing = await files.list_directory("src")
    assert listing == ["src/a.py", "src/b.py"]
    hits = await files.search_code(r"def add")
    assert hits[0].path == "src/a.py"
    assert hits[0].line_number == 1
    tree = await files.list_tree()
    assert "src/" in tree
    assert "src/a.py" in tree


async def test_missing_file(tmp_path: Path) -> None:
    _space, files = _workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="not found"):
        await files.read_file("missing.py")

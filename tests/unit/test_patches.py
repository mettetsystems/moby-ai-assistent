from pathlib import Path

import pytest

from mobyai.execution.errors import PatchError, PathEscapeError
from mobyai.execution.patches import apply_patch
from mobyai.execution.workspace import Workspace


def _space(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    return Workspace(root)


def test_apply_modify_hunk(tmp_path: Path) -> None:
    space = _space(tmp_path)
    target = space.root / "src" / "hello.py"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")
    changed = apply_patch(
        space,
        """\
--- a/src/hello.py
+++ b/src/hello.py
@@ -1 +1 @@
-hello
+hello world
""",
    )
    assert changed == ["src/hello.py"]
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_create_file_from_dev_null(tmp_path: Path) -> None:
    space = _space(tmp_path)
    apply_patch(
        space,
        """\
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+alpha
+beta
""",
    )
    assert (space.root / "src" / "new.py").read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_rejects_mismatched_context(tmp_path: Path) -> None:
    space = _space(tmp_path)
    target = space.root / "file.txt"
    target.write_text("actual\n", encoding="utf-8")
    with pytest.raises(PatchError, match="does not match"):
        apply_patch(
            space,
            """\
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-expected
+other
""",
        )


def test_rejects_path_escape_in_header(tmp_path: Path) -> None:
    space = _space(tmp_path)
    with pytest.raises(PatchError, match="not workspace-relative"):
        apply_patch(
            space,
            """\
--- a/../secret.py
+++ b/../secret.py
@@ -0,0 +1 @@
+nope
""",
        )
    with pytest.raises((PatchError, PathEscapeError)):
        apply_patch(
            space,
            """\
--- a/../../etc/passwd
+++ b/../../etc/passwd
@@ -0,0 +1 @@
+nope
""",
        )

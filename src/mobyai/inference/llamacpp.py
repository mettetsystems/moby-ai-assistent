"""llama.cpp runtime discovery and install. The harness does not import llama.cpp."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

INSTALL_SCRIPT_URL = "https://llama.app/install.sh"
_HOME_APP = Path.home() / ".llama-app" / "llama"
_HOME_BIN = Path.home() / ".local" / "bin" / "llama"
_COMMAND_NAMES = ("llama", "llama-server")


class LlamaRuntimeError(Exception):
    """llama.cpp binary is missing or the official installer failed."""


def find_llama_binary() -> Path | None:
    for name in _COMMAND_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (_HOME_BIN, _HOME_APP):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_launch_argv(argv: Sequence[str]) -> list[str]:
    rendered = [str(part) for part in argv]
    if not rendered:
        return rendered
    first = rendered[0]
    if Path(first).expanduser().is_file() or os.sep in first or first.startswith("."):
        return rendered
    if first not in _COMMAND_NAMES:
        return rendered
    found = find_llama_binary()
    if found is not None:
        rendered[0] = str(found)
    return rendered


def ensure_llama_runtime(
    *,
    stdout: TextIO,
    skip: bool = False,
    installer: Callable[[], Path] | None = None,
) -> Path:
    existing = find_llama_binary()
    if existing is not None:
        print(f"llama.cpp  {existing}", file=stdout)
        return existing
    if skip:
        raise LlamaRuntimeError(
            "llama.cpp is not on PATH. Install from https://llama.app or re-run setup."
        )
    print("Installing llama.cpp (official llama.app installer)…", file=stdout)
    install = installer or _run_official_installer
    path = install()
    found = find_llama_binary() or path
    if found is None or not Path(found).is_file():
        raise LlamaRuntimeError("llama.cpp installer finished but no llama binary was found")
    print(f"llama.cpp  {found}", file=stdout)
    return Path(found)


def _run_official_installer() -> Path:
    with tempfile.TemporaryDirectory(prefix="mobyai-llama-") as tmp:
        script = Path(tmp) / "install.sh"
        try:
            urllib.request.urlretrieve(INSTALL_SCRIPT_URL, script)
        except OSError as exc:
            raise LlamaRuntimeError(
                f"Could not download {INSTALL_SCRIPT_URL}: {exc}"
            ) from exc
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        try:
            subprocess.run(
                ["sh", str(script)],
                check=True,
                cwd=tmp,
                env=env,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise LlamaRuntimeError(f"llama.cpp installer failed: {exc}") from exc
    found = find_llama_binary()
    if found is None:
        raise LlamaRuntimeError("llama.cpp installer did not place a llama binary")
    return found

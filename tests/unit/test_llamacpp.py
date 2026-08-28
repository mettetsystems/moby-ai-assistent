from __future__ import annotations

from pathlib import Path

from mobyai.inference.llamacpp import find_llama_binary, resolve_launch_argv


def test_resolve_launch_argv_leaves_unknown_binary() -> None:
    argv = ["vllm", "serve", "m"]
    assert resolve_launch_argv(argv) == argv


def test_resolve_launch_argv_substitutes_discovered_llama(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "llama"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(
        "mobyai.inference.llamacpp.find_llama_binary", lambda: binary
    )
    assert resolve_launch_argv(["llama", "serve", "-hf", "org/name"])[0] == str(binary)


def test_find_llama_binary_uses_path(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "llama"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("mobyai.inference.llamacpp._HOME_APP", tmp_path / "missing")
    monkeypatch.setattr("mobyai.inference.llamacpp._HOME_BIN", tmp_path / "missing-bin")
    found = find_llama_binary()
    assert found == binary

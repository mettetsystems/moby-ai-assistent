from __future__ import annotations

from pathlib import Path

import pytest

from mobyai.inference.llamacpp import (
    detect_llama_backend,
    find_llama_binary,
    render_mobyai_env,
    resolve_launch_argv,
)


def test_detect_llama_backend_cuda(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "llama"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "mobyai.inference.llamacpp.subprocess.run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {"stdout": "libggml-cuda.so => /lib/libggml-cuda.so", "returncode": 0},
        )(),
    )
    assert detect_llama_backend(binary) == "cuda"


def test_detect_llama_backend_hip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "mobyai.inference.llamacpp.subprocess.run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {"stdout": "libggml-hip.so => /lib/libggml-hip.so", "returncode": 0},
        )(),
    )
    assert detect_llama_backend(binary) == "hip"


def test_find_llama_binary_prefers_cuda_over_hip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cuda = tmp_path / "cuda-llama"
    hip = tmp_path / "hip-llama"
    cuda.write_text("", encoding="utf-8")
    hip.write_text("", encoding="utf-8")
    cuda.chmod(0o755)
    hip.chmod(0o755)

    def fake_backend(path: Path) -> str:
        if path.name == "cuda-llama":
            return "cuda"
        return "hip"

    monkeypatch.setattr("mobyai.inference.llamacpp.llama_candidate_paths", lambda: [hip, cuda])
    monkeypatch.setattr("mobyai.inference.llamacpp.detect_llama_backend", fake_backend)
    monkeypatch.setattr("mobyai.inference.llamacpp.detect_nvidia_gpus", lambda: [object()])
    assert find_llama_binary(prefer_cuda=True) == cuda


def test_render_mobyai_env_puts_local_bin_first(tmp_path: Path) -> None:
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    llama = local_bin / "llama"
    llama.write_text("", encoding="utf-8")
    text = render_mobyai_env(llama_binary=llama)
    assert str(local_bin) in text
    assert "export PATH=" in text


def test_resolve_launch_argv_substitutes_discovered_llama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "llama"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(
        "mobyai.inference.llamacpp.find_llama_binary", lambda **kwargs: binary
    )
    assert resolve_launch_argv(["llama", "serve", "-hf", "org/name"])[0] == str(binary)

"""GPU discovery and role-to-device assignment for setup."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    memory_mib: int


def detect_nvidia_gpus() -> list[GpuInfo]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    gpus: list[GpuInfo] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        index_text, name, memory_text = parts
        if not index_text.lstrip("-").isdigit():
            continue
        memory_digits = "".join(ch for ch in memory_text if ch.isdigit())
        if not memory_digits:
            continue
        gpus.append(
            GpuInfo(
                index=int(index_text),
                name=name,
                memory_mib=int(memory_digits),
            )
        )
    return sorted(gpus, key=lambda gpu: gpu.index)


def assign_role_devices(gpus: list[GpuInfo]) -> dict[str, str]:
    """Map planner/worker to the largest GPU; orchestrator to the smallest."""
    fallback = {"planner": "0", "orchestrator": "1", "worker": "0"}
    if not gpus:
        return fallback

    by_vram = sorted(gpus, key=lambda gpu: gpu.memory_mib, reverse=True)
    primary = by_vram[0]
    if len(by_vram) == 1:
        device = str(primary.index)
        return {"planner": device, "orchestrator": device, "worker": device}

    smallest = by_vram[-1]
    return {
        "planner": str(primary.index),
        "orchestrator": str(smallest.index),
        "worker": str(primary.index),
    }


def format_gpu_table(gpus: list[GpuInfo]) -> str:
    if not gpus:
        return "  (no NVIDIA GPUs detected via nvidia-smi)"
    lines = ["  index  name                          VRAM"]
    for gpu in gpus:
        vram_gib = gpu.memory_mib / 1024
        lines.append(f"  {gpu.index:<5}  {gpu.name:<28}  {vram_gib:.1f} GiB")
    return "\n".join(lines)


def format_role_layout(assignments: dict[str, str], gpus: list[GpuInfo]) -> str:
    by_index = {gpu.index: gpu for gpu in gpus}

    def label(device: str) -> str:
        if device.lstrip("-").isdigit():
            gpu = by_index.get(int(device))
            if gpu is not None:
                return f"GPU {device} ({gpu.name}, {gpu.memory_mib // 1024} GiB)"
        return f"device {device}"

    return (
        f"  planner       {label(assignments['planner'])} — 32B-class model\n"
        f"  orchestrator  {label(assignments['orchestrator'])} — 7B-class model\n"
        f"  worker        {label(assignments['worker'])} — 35B MoE model"
    )


def print_hardware_scan(*, stdout: TextIO, gpus: list[GpuInfo], assignments: dict[str, str]) -> None:
    print("Detected GPUs", file=stdout)
    print(format_gpu_table(gpus), file=stdout)
    print("", file=stdout)
    print("Proposed GPU layout (largest GPU for planner + worker):", file=stdout)
    print(format_role_layout(assignments, gpus), file=stdout)

from __future__ import annotations

from mobyai.hardware import GpuInfo, assign_role_devices


def test_assign_role_devices_single_gpu() -> None:
    gpus = [GpuInfo(index=0, name="RTX 5090", memory_mib=32607)]
    assert assign_role_devices(gpus) == {
        "planner": "0",
        "orchestrator": "0",
        "worker": "0",
    }


def test_assign_role_devices_dual_gpu_puts_heavy_on_largest() -> None:
    gpus = [
        GpuInfo(index=0, name="RTX 2070", memory_mib=8192),
        GpuInfo(index=1, name="RTX 5090", memory_mib=32607),
    ]
    assert assign_role_devices(gpus) == {
        "planner": "1",
        "orchestrator": "0",
        "worker": "1",
    }


def test_assign_role_devices_no_gpus_uses_reference() -> None:
    assert assign_role_devices([]) == {
        "planner": "0",
        "orchestrator": "1",
        "worker": "0",
    }

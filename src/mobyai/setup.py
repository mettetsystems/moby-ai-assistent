"""First-run setup wizard. Writes config, installs llama.cpp, pulls GGUF weights, tests endpoints."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, TextIO

from mobyai.inference.huggingface import (
    HubDownloadError,
    hub_repo_ids,
    mask_token,
    pull_hub_models,
    token_from_file,
    write_token_file,
)
from mobyai.hardware import (
    assign_role_devices,
    detect_nvidia_gpus,
    print_hardware_scan,
)
from mobyai.inference.llamacpp import (
    LlamaRuntimeError,
    apply_mobyai_env,
    describe_llama_binary,
    ensure_llama_runtime,
    find_llama_binary,
    write_mobyai_env,
)
from mobyai.inference.service_manager import GpuServiceManager
from mobyai.models.config import AgentRole, AppConfig, ServiceMode

DEFAULT_OUTPUT = Path("config.yaml")
DEFAULT_WORKSPACE = "~/MobyAI/projects"
DEFAULT_DATABASE = "~/MobyAI/mobyai.db"
DEFAULT_HF_TOKEN_FILE = "~/MobyAI/secrets/hf_token"
DEFAULT_PRESET = "default"
DEFAULT_MODE = ServiceMode.MANAGED.value

PRESETS = frozenset({"default", "external", "custom"})

# Reference stack from the operator spec. Overridable at every prompt and flag.
DEFAULT_ROLES: dict[str, dict[str, str]] = {
    "planner": {
        "model": "Qwen/Qwen3-32B-GGUF:Q4_K_M",
        "endpoint": "http://127.0.0.1:9802",
        "device": "0",
    },
    "orchestrator": {
        "model": "Qwen/Qwen2.5-7B-Instruct-GGUF:Q5_K_M",
        "endpoint": "http://127.0.0.1:9801",
        "device": "1",
    },
    "worker": {
        "model": "unsloth/Qwen3.5-35B-A3B-GGUF:Q4_K_M",
        "endpoint": "http://127.0.0.1:9800",
        "device": "0",
    },
}


def default_role_devices() -> dict[str, str]:
    return assign_role_devices(detect_nvidia_gpus())

_LEGACY_PLACEHOLDERS = frozenset(
    {
        "configurable-planner-model",
        "configurable-small-model",
        "configurable-coding-model",
    }
)


class SetupError(Exception):
    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RoleAnswers:
    model: str
    endpoint: str
    device: str


@dataclass(frozen=True)
class SetupAnswers:
    output: Path
    workspace: str
    database: str
    mode: str
    planner: RoleAnswers
    orchestrator: RoleAnswers
    worker: RoleAnswers
    create_directories: bool
    hf_token: str
    hf_token_file: str
    preset: str
    download_models: bool
    install_runtime: bool
    endpoint_test: bool
    llama_binary: str | None = None


def run_setup(
    *,
    output: Path | None = None,
    non_interactive: bool = False,
    force: bool = False,
    mode: str | None = None,
    workspace: str | None = None,
    database: str | None = None,
    planner_model: str | None = None,
    planner_endpoint: str | None = None,
    planner_device: str | None = None,
    orchestrator_model: str | None = None,
    orchestrator_endpoint: str | None = None,
    orchestrator_device: str | None = None,
    worker_model: str | None = None,
    worker_endpoint: str | None = None,
    worker_device: str | None = None,
    create_directories: bool | None = None,
    hf_token: str | None = None,
    hf_token_file: str | None = None,
    skip_hf_token: bool = False,
    preset: str | None = None,
    download_models: bool | None = None,
    skip_download: bool = False,
    skip_runtime: bool = False,
    skip_endpoint_test: bool = False,
    llama_installer: Callable[[], Path] | None = None,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    interactive = not non_interactive
    if interactive and not _can_prompt(stdin):
        raise SetupError(
            "stdin is not a terminal. Re-run with --non-interactive, or use "
            "./scripts/setup.sh"
        )

    _print_preamble(stdout, interactive=interactive)
    gpus = detect_nvidia_gpus()
    role_devices = default_role_devices()
    answers = collect_answers(
        output=output,
        mode=mode,
        workspace=workspace,
        database=database,
        planner_model=planner_model,
        planner_endpoint=planner_endpoint,
        planner_device=planner_device,
        orchestrator_model=orchestrator_model,
        orchestrator_endpoint=orchestrator_endpoint,
        orchestrator_device=orchestrator_device,
        worker_model=worker_model,
        worker_endpoint=worker_endpoint,
        worker_device=worker_device,
        create_directories=create_directories,
        hf_token=hf_token,
        hf_token_file=hf_token_file,
        skip_hf_token=skip_hf_token,
        preset=preset,
        download_models=download_models,
        skip_download=skip_download,
        skip_runtime=skip_runtime,
        skip_endpoint_test=skip_endpoint_test,
        interactive=interactive,
        force=force,
        gpus=gpus,
        role_devices=role_devices,
        stdin=stdin,
        stdout=stdout,
    )
    write_setup(answers, stdout=stdout)
    llama_binary = _provision_platform(
        answers,
        stdout=stdout,
        llama_installer=llama_installer,
    )
    if llama_binary is not None:
        answers = replace(answers, llama_binary=str(llama_binary))
        answers.output.write_text(render_config_yaml(answers), encoding="utf-8")
        print(f"updated    {answers.output.resolve()}  launch -> {llama_binary}", file=stdout)
    _print_next_steps(answers, stdout)
    return 0


def collect_answers(
    *,
    output: Path | None,
    mode: str | None,
    workspace: str | None,
    database: str | None,
    planner_model: str | None,
    planner_endpoint: str | None,
    planner_device: str | None,
    orchestrator_model: str | None,
    orchestrator_endpoint: str | None,
    orchestrator_device: str | None,
    worker_model: str | None,
    worker_endpoint: str | None,
    worker_device: str | None,
    create_directories: bool | None,
    hf_token: str | None,
    hf_token_file: str | None,
    skip_hf_token: bool,
    preset: str | None,
    download_models: bool | None,
    skip_download: bool,
    skip_runtime: bool,
    skip_endpoint_test: bool,
    interactive: bool,
    force: bool,
    gpus: list,
    role_devices: dict[str, str],
    stdin: TextIO,
    stdout: TextIO,
) -> SetupAnswers:
    _wizard_step(1, "Environment scan", stdout)
    print_hardware_scan(stdout=stdout, gpus=gpus, assignments=role_devices)
    apply_mobyai_env()
    llama = find_llama_binary(prefer_cuda=bool(gpus))
    if llama is not None:
        print(f"llama.cpp  {describe_llama_binary(llama)}", file=stdout)
    elif gpus:
        print(
            "llama.cpp  not found with CUDA support (setup will install from llama.app)",
            file=stdout,
        )
    else:
        print("llama.cpp  not found on PATH (setup can install from llama.app)", file=stdout)
    print("", file=stdout)

    _wizard_step(2, "Config paths", stdout)
    dest = Path(
        _value(
            None if output is None else str(output),
            "Config file to write",
            str(DEFAULT_OUTPUT),
            interactive=interactive,
            stdin=stdin,
            stdout=stdout,
        )
    ).expanduser()
    if dest.exists() and not force:
        if not interactive:
            raise SetupError(
                f"Config already exists: {dest}  (pass --force to overwrite)"
            )
        overwrite = _ask_bool(
            f"{dest} already exists. Overwrite?",
            default=False,
            stdin=stdin,
            stdout=stdout,
        )
        if not overwrite:
            raise SetupError(f"Refusing to overwrite {dest}")

    print("", file=stdout)
    print("Paths", file=stdout)
    print(
        "  workspace_root — directories created for each coding project",
        file=stdout,
    )
    print("  database_path  — SQLite file; this is crash-recovery state", file=stdout)
    workspace_value = _value(
        workspace,
        "Project workspace root",
        DEFAULT_WORKSPACE,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )
    database_value = _value(
        database,
        "SQLite database path",
        DEFAULT_DATABASE,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )

    token_file_value, token_value = _collect_huggingface(
        token=hf_token,
        token_file=hf_token_file,
        skip=skip_hf_token,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )

    _wizard_step(3, "Runtime deployment", stdout)
    print(
        "  default  — managed llama.cpp (CUDA on NVIDIA), auto GPU layout from nvidia-smi,",
        file=stdout,
    )
    print("             reference Hugging Face models (Enter on each field to keep)", file=stdout)
    print(
        "  external — same models and devices; you start the three HTTP servers",
        file=stdout,
    )
    print(
        "  custom   — choose mode, then type any model id / endpoint / device",
        file=stdout,
    )
    preset_value = _value(
        preset,
        "Deployment preset (default, external, or custom)",
        DEFAULT_PRESET,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    ).strip().lower()
    if preset_value not in PRESETS:
        raise SetupError("Deployment preset must be 'default', 'external', or 'custom'")

    if mode is not None:
        mode_value = mode.strip().lower()
    elif preset_value == "external":
        mode_value = ServiceMode.EXTERNAL.value
    elif preset_value == "default":
        mode_value = ServiceMode.MANAGED.value
    else:
        print("", file=stdout)
        print("Inference mode", file=stdout)
        print(
            "  external — you start Planner/Orchestrator/Worker servers; MobyAI only healthchecks",
            file=stdout,
        )
        print(
            "  managed  — MobyAI spawns llama.cpp with CUDA_VISIBLE_DEVICES from hardware",
            file=stdout,
        )
        mode_value = _value(
            None,
            "Service mode (external or managed)",
            DEFAULT_MODE,
            interactive=interactive,
            stdin=stdin,
            stdout=stdout,
        ).strip().lower()
    if mode_value not in {ServiceMode.EXTERNAL.value, ServiceMode.MANAGED.value}:
        raise SetupError("Service mode must be 'external' or 'managed'")

    _wizard_step(4, "GPU layout", stdout)
    use_auto_layout = _confirm_gpu_layout(
        role_devices=role_devices,
        gpus=gpus,
        planner_device=planner_device,
        orchestrator_device=orchestrator_device,
        worker_device=worker_device,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )

    _wizard_step(5, "Models and endpoints", stdout)
    print("Endpoints must match GET /v1/models on that server after it starts.", file=stdout)
    role_defaults = {
        role: {
            **DEFAULT_ROLES[role],
            "device": role_devices[role] if use_auto_layout else DEFAULT_ROLES[role]["device"],
        }
        for role in DEFAULT_ROLES
    }
    planner = _collect_role(
        "planner",
        defaults=role_defaults["planner"],
        model=planner_model,
        endpoint=planner_endpoint,
        device=planner_device if not use_auto_layout else (planner_device or role_devices["planner"]),
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )
    orchestrator = _collect_role(
        "orchestrator",
        defaults=role_defaults["orchestrator"],
        model=orchestrator_model,
        endpoint=orchestrator_endpoint,
        device=orchestrator_device
        if not use_auto_layout
        else (orchestrator_device or role_devices["orchestrator"]),
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )
    worker = _collect_role(
        "worker",
        defaults=role_defaults["worker"],
        model=worker_model,
        endpoint=worker_endpoint,
        device=worker_device if not use_auto_layout else (worker_device or role_devices["worker"]),
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )

    _wizard_step(6, "Platform provision", stdout)
    if create_directories is None:
        make_dirs = (
            _ask_bool(
                "Create workspace and database parent directories now?",
                default=True,
                stdin=stdin,
                stdout=stdout,
            )
            if interactive
            else True
        )
    else:
        make_dirs = create_directories

    if skip_download:
        pull = False
    elif download_models is True:
        pull = True
    elif download_models is False:
        pull = False
    elif interactive:
        print(
            "  Installs CUDA llama.cpp (llama.app), writes ~/MobyAI/env.sh for PATH,",
            file=stdout,
        )
        print("  downloads GGUF weights (often tens of GB),", file=stdout)
        print("  then starts each role once and checks /v1/models.", file=stdout)
        print("  After that, `mobyai start` should find servers or spawn them.", file=stdout)
        pull = _ask_bool(
            "Install llama.cpp, download models, and test endpoints now?",
            default=True,
            stdin=stdin,
            stdout=stdout,
        )
    else:
        pull = False

    llama_path: str | None = None
    if pull and not skip_runtime:
        apply_mobyai_env()
        found = find_llama_binary(prefer_cuda=bool(gpus))
        if found is not None:
            llama_path = str(found)

    return SetupAnswers(
        output=dest,
        workspace=workspace_value,
        database=database_value,
        mode=mode_value,
        planner=planner,
        orchestrator=orchestrator,
        worker=worker,
        create_directories=make_dirs,
        hf_token=token_value,
        hf_token_file=token_file_value,
        preset=preset_value,
        download_models=pull,
        install_runtime=pull and not skip_runtime,
        endpoint_test=pull and not skip_endpoint_test,
        llama_binary=llama_path,
    )


def write_setup(answers: SetupAnswers, *, stdout: TextIO) -> Path:
    text = render_config_yaml(answers)
    AppConfig.from_yaml_text(text)
    answers.output.parent.mkdir(parents=True, exist_ok=True)
    answers.output.write_text(text, encoding="utf-8")
    print(f"wrote      {answers.output.resolve()}", file=stdout)
    if answers.hf_token:
        token_path = write_token_file(answers.hf_token_file, answers.hf_token)
        print(f"hf token   {token_path}  (mode 0600, not stored in YAML)", file=stdout)
    elif Path(answers.hf_token_file).expanduser().is_file():
        print(
            f"hf token   {Path(answers.hf_token_file).expanduser()}  (kept existing file)",
            file=stdout,
        )
    else:
        print(
            f"hf token   (none written; put a token in {answers.hf_token_file} or set HF_TOKEN)",
            file=stdout,
        )
    if answers.create_directories:
        workspace = Path(answers.workspace).expanduser()
        database = Path(answers.database).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)
        database.parent.mkdir(parents=True, exist_ok=True)
        print(f"workspaces {workspace}", file=stdout)
        print(f"database   {database.parent}  (file created on first init)", file=stdout)
    return answers.output


def _provision_platform(
    answers: SetupAnswers,
    *,
    stdout: TextIO,
    llama_installer: Callable[[], Path] | None,
) -> Path | None:
    llama_binary: Path | None = None
    if answers.install_runtime:
        _wizard_step(7, "Install llama.cpp", stdout)
        try:
            llama_binary = ensure_llama_runtime(
                stdout=stdout,
                installer=llama_installer,
            )
        except LlamaRuntimeError as exc:
            raise SetupError(str(exc)) from exc
    elif answers.llama_binary:
        llama_binary = Path(answers.llama_binary)
        write_mobyai_env(llama_binary=llama_binary, stdout=stdout)
    _download_weights(answers, stdout=stdout)
    if answers.endpoint_test:
        apply_mobyai_env()
        _endpoint_test(answers, stdout=stdout)
    return llama_binary


def _download_weights(answers: SetupAnswers, *, stdout: TextIO) -> None:
    if not answers.download_models:
        return
    ids = hub_repo_ids(
        answers.planner.model,
        answers.orchestrator.model,
        answers.worker.model,
    )
    if not ids:
        print("No Hugging Face repo ids to download (need org/name).", file=stdout)
        return
    token = (answers.hf_token or "").strip() or token_from_file(answers.hf_token_file)
    if not token:
        token = (
            os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""
        ).strip() or None
    print("", file=stdout)
    print("Downloading model weights into the Hugging Face cache.", file=stdout)
    try:
        pull_hub_models(ids, token=token, stdout=stdout)
    except HubDownloadError as exc:
        raise SetupError(str(exc)) from exc


def _run_async(factory):  # type: ignore[no-untyped-def]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["ok"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    thread = threading.Thread(target=worker, name="mobyai-setup-probe")
    thread.start()
    thread.join()
    if "err" in box:
        raise box["err"]  # type: ignore[misc]
    return box.get("ok")


def _endpoint_test(answers: SetupAnswers, *, stdout: TextIO) -> None:
    if answers.mode != ServiceMode.MANAGED.value:
        print(
            "Endpoint test skipped (external mode — start your own servers, then `mobyai doctor`).",
            file=stdout,
        )
        return
    config = AppConfig.from_yaml(answers.output)
    print("", file=stdout)
    print("Testing llama.cpp endpoints (first start loads weights from the HF cache)…", file=stdout)

    def on_progress(role: AgentRole, detail: str) -> None:
        print(f"  {role.value:<14} {detail}", file=stdout, flush=True)

    async def _probe() -> None:
        manager = GpuServiceManager(config)
        manager.set_progress(on_progress)
        try:
            for role in AgentRole:
                endpoint = await manager.ensure_role_available(role)
                print(
                    f"  {role.value:<14} ok  {endpoint.base_url}",
                    file=stdout,
                    flush=True,
                )
            for role in AgentRole:
                await manager.stop_role(role)
        finally:
            manager.set_progress(None)
            await manager.aclose()

    try:
        _run_async(_probe)
    except Exception as exc:
        raise SetupError(f"Endpoint test failed: {exc}") from exc
    print("Endpoint test passed. `mobyai start` will reuse a live server or spawn llama.cpp.", file=stdout)


def render_config_yaml(answers: SetupAnswers) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    planner_launch = _launch_block(answers.mode, answers.llama_binary, ctx=32768)
    orchestrator_launch = _launch_block(answers.mode, answers.llama_binary, ctx=16384)
    worker_launch = _launch_block(answers.mode, answers.llama_binary, ctx=65536)
    return f"""# Generated by `mobyai setup` on {generated}
# Field-by-field reference: docs/setup.md
# After editing: mobyai --config {answers.output} config validate
# Preset: {answers.preset}   mode: {answers.mode}

project:
  # Root for per-project workspaces. Each `mobyai init --name` creates a subdirectory here
  # unless you pass --workspace. The harness refuses to write outside a project workspace.
  workspace_root: {_yaml_scalar(answers.workspace)}

runtime:
  # First failure retries the same task. Second failure escalates (DAG branch patch).
  max_task_attempts: 2
  command_timeout_seconds: 600
  checkpointing: true
  max_output_bytes: 1048576
  max_log_bytes: 52428800
  max_schema_repair_attempts: 2
  inference_timeout_seconds: 300
  max_worker_turns: 8
  max_planner_inspect_turns: 3

context:
  worker_token_budget: 48000
  log_tail_lines: 150
  prefer_import_contracts: true

persistence:
  # Crash recovery lives here, not in chat history. `mobyai resume` reloads this file.
  database_path: {_yaml_scalar(answers.database)}

huggingface:
  # Gated Hub downloads. Setup writes the token to this file (mode 0600).
  # Do not paste the token into YAML. Override with HF_TOKEN in the environment.
  token_file: {_yaml_scalar(answers.hf_token_file)}

services:
  # external: you already started the three OpenAI-compatible servers.
  # managed:  MobyAI spawns llama.cpp (`llama serve`) with CUDA_VISIBLE_DEVICES from hardware.
  mode: {answers.mode}
  health_timeout_seconds: 600
  health_poll_interval_seconds: 0.5
  stop_timeout_seconds: 15
  # When planner and worker share a device, persist state then swap the CUDA process.
  swap_shared_device: true

hardware:
  # Device indices are configuration, not code constants. Use cpu to skip CUDA.
  planner:
    device: {_yaml_device(answers.planner.device)}
  orchestrator:
    device: {_yaml_device(answers.orchestrator.device)}
  worker:
    device: {_yaml_device(answers.worker.device)}

models:
  planner:
    provider: openai_compatible
    engine: llama.cpp
    model: {_yaml_scalar(answers.planner.model)}
    gpu: {_yaml_gpu(answers.planner.device)}
    endpoint: {_yaml_scalar(answers.planner.endpoint)}
    quantization: q4{planner_launch}
  orchestrator:
    provider: openai_compatible
    engine: llama.cpp
    model: {_yaml_scalar(answers.orchestrator.model)}
    gpu: {_yaml_gpu(answers.orchestrator.device)}
    endpoint: {_yaml_scalar(answers.orchestrator.endpoint)}
    quantization: q5{orchestrator_launch}
  worker:
    provider: openai_compatible
    engine: llama.cpp
    model: {_yaml_scalar(answers.worker.model)}
    gpu: {_yaml_gpu(answers.worker.device)}
    endpoint: {_yaml_scalar(answers.worker.endpoint)}
    quantization: q4{worker_launch}
"""


def _collect_huggingface(
    *,
    token: str | None,
    token_file: str | None,
    skip: bool,
    interactive: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> tuple[str, str]:
    print("", file=stdout)
    print("Hugging Face token", file=stdout)
    print(
        "  Used to download gated weights (vLLM / huggingface_hub). Optional for public models.",
        file=stdout,
    )
    print(
        f"  Stored at {DEFAULT_HF_TOKEN_FILE} (mode 0600). Never written into YAML.",
        file=stdout,
    )
    print("  You can also export HF_TOKEN in the shell instead of pasting it here.", file=stdout)

    file_value = _value(
        token_file,
        "Token file path",
        DEFAULT_HF_TOKEN_FILE,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    ).strip()
    existing_file = token_from_file(file_value)
    env_token = (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip()

    if skip:
        return file_value, ""
    if token is not None:
        return file_value, token.strip()

    if not interactive:
        if env_token:
            return file_value, env_token
        return file_value, ""

    if existing_file:
        keep = _ask_bool(
            f"Found existing token {mask_token(existing_file)}. Keep it?",
            default=True,
            stdin=stdin,
            stdout=stdout,
        )
        if keep:
            return file_value, ""
    elif env_token:
        keep = _ask_bool(
            f"Using HF_TOKEN from the environment {mask_token(env_token)}. Write a copy to the token file?",
            default=True,
            stdin=stdin,
            stdout=stdout,
        )
        if keep:
            return file_value, env_token
        return file_value, ""

    entered = _ask_secret(
        "Hugging Face token",
        stdin=stdin,
        stdout=stdout,
    )
    return file_value, entered


def _collect_role(
    role: str,
    *,
    defaults: dict[str, str],
    model: str | None,
    endpoint: str | None,
    device: str | None,
    interactive: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> RoleAnswers:
    print("", file=stdout)
    print(f"{role.title()} role", file=stdout)
    return RoleAnswers(
        model=_value(
            model,
            f"{role} Hugging Face GGUF spec (org/name:QUANT)",
            defaults["model"],
            interactive=interactive,
            stdin=stdin,
            stdout=stdout,
        ).strip(),
        endpoint=_value(
            endpoint,
            f"{role} OpenAI-compatible endpoint",
            defaults["endpoint"],
            interactive=interactive,
            stdin=stdin,
            stdout=stdout,
        ).strip(),
        device=_value(
            device,
            f"{role} device (GPU index or cpu)",
            defaults["device"],
            interactive=interactive,
            stdin=stdin,
            stdout=stdout,
        ).strip(),
    )


def _wizard_step(number: int, title: str, stdout: TextIO) -> None:
    print("", file=stdout)
    print(f"Step {number}/7 — {title}", file=stdout)
    print("-" * (len(title) + 12), file=stdout)


def _confirm_gpu_layout(
    *,
    role_devices: dict[str, str],
    gpus: list,
    planner_device: str | None,
    orchestrator_device: str | None,
    worker_device: str | None,
    interactive: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> bool:
    explicit = any(
        value is not None
        for value in (planner_device, orchestrator_device, worker_device)
    )
    if explicit:
        print("Using GPU indices from command-line flags.", file=stdout)
        return False
    if not gpus:
        print("No NVIDIA GPUs detected; keeping reference device indices 0/1/0.", file=stdout)
        return False
    print_hardware_scan(stdout=stdout, gpus=gpus, assignments=role_devices)
    if not interactive:
        print("Auto-selected GPU layout from nvidia-smi.", file=stdout)
        return True
    return _ask_bool(
        "Use the proposed GPU layout?",
        default=True,
        stdin=stdin,
        stdout=stdout,
    )


def _value(
    provided: str | None,
    prompt: str,
    default: str,
    *,
    interactive: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> str:
    if provided is not None:
        return provided
    if not interactive:
        return default
    return _ask(prompt, default, stdin=stdin, stdout=stdout)


def _ask(prompt: str, default: str, *, stdin: TextIO, stdout: TextIO) -> str:
    suffix = f" [{default}]" if default else ""
    print(f"{prompt}{suffix}: ", end="", file=stdout, flush=True)
    line = stdin.readline()
    if line == "":
        raise SetupError("Unexpected end of input while prompting")
    text = line.strip()
    return text if text else default


def _ask_secret(prompt: str, *, stdin: TextIO, stdout: TextIO) -> str:
    print(f"{prompt} [leave blank to skip]: ", end="", file=stdout, flush=True)
    if stdin is sys.stdin and _can_prompt(stdin):
        try:
            import getpass

            return getpass.getpass(prompt="", stream=stdout).strip()
        except (EOFError, OSError):
            pass
    line = stdin.readline()
    if line == "":
        raise SetupError("Unexpected end of input while prompting")
    return line.strip()


def _ask_bool(prompt: str, *, default: bool, stdin: TextIO, stdout: TextIO) -> bool:
    hint = "Y/n" if default else "y/N"
    print(f"{prompt} [{hint}]: ", end="", file=stdout, flush=True)
    line = stdin.readline()
    if line == "":
        raise SetupError("Unexpected end of input while prompting")
    text = line.strip().lower()
    if not text:
        return default
    if text in {"y", "yes"}:
        return True
    if text in {"n", "no"}:
        return False
    raise SetupError("Please answer yes or no")


def _can_prompt(stdin: TextIO) -> bool:
    isatty = getattr(stdin, "isatty", None)
    if callable(isatty):
        try:
            return bool(isatty())
        except OSError:
            return False
    return False


def _print_preamble(stdout: TextIO, *, interactive: bool) -> None:
    print("MobyAI setup wizard", file=stdout)
    print(
        "Guides you through config, NVIDIA GPU layout, CUDA llama.cpp, Hugging Face weights, "
        "and a live endpoint test.",
        file=stdout,
    )
    print("It does not leave the GPU servers running; `mobyai start` does that.", file=stdout)
    print(
        "Default stack: managed llama.cpp (CUDA), auto GPU layout, "
        "Qwen3-32B / Qwen2.5-7B / Qwen3.5-35B-A3B GGUF.",
        file=stdout,
    )
    print("Press Enter to keep a [default], or type a custom value.", file=stdout)
    if not interactive:
        print(
            "Non-interactive: defaults plus any flags you passed. "
            "Pass --download-models to install llama.cpp, pull weights, and test endpoints.",
            file=stdout,
        )
    print("", file=stdout)
    print("Config search order after this file exists:", file=stdout)
    print("  1. --config PATH", file=stdout)
    print("  2. $MOBYAI_CONFIG", file=stdout)
    print("  3. ./config.yaml", file=stdout)
    print("  4. ./config/default.yaml", file=stdout)
    print("  5. ~/MobyAI/config.yaml", file=stdout)


def _print_next_steps(answers: SetupAnswers, stdout: TextIO) -> None:
    cfg = str(answers.output)
    print("", file=stdout)
    placeholders = [
        role
        for role, answers_role in (
            ("planner", answers.planner),
            ("orchestrator", answers.orchestrator),
            ("worker", answers.worker),
        )
        if answers_role.model in _LEGACY_PLACEHOLDERS
    ]
    if placeholders:
        print(
            "WARNING  legacy placeholder model ids remain for: " + ", ".join(placeholders),
            file=stdout,
        )
        print("", file=stdout)
    token_path = Path(answers.hf_token_file).expanduser()
    if answers.mode == ServiceMode.MANAGED.value and not (
        answers.hf_token or token_path.is_file() or os.environ.get("HF_TOKEN")
    ):
        print(
            "WARNING  no Hugging Face token on disk or in HF_TOKEN.",
            file=stdout,
        )
        print(
            "         Public models still download; gated repos will fail at start.",
            file=stdout,
        )
        print("", file=stdout)
    print("Next steps", file=stdout)
    print(f"  1. Shell env         source ~/MobyAI/env.sh", file=stdout)
    print(f"  2. Review            {cfg}", file=stdout)
    print(f"  3. Validate          mobyai --config {cfg} config validate", file=stdout)
    if answers.mode == ServiceMode.EXTERNAL.value:
        print("  4. Start your three OpenAI-compatible servers on the configured ports.", file=stdout)
        print(f"  5. Healthcheck       mobyai --config {cfg} doctor", file=stdout)
        print(f"  6. Mark ready        mobyai --config {cfg} start", file=stdout)
    else:
        if answers.download_models:
            print(
                "  4. CUDA llama.cpp is installed. Weights are in the Hugging Face cache.",
                file=stdout,
            )
        else:
            print(
                "  4. Re-run setup and accept provision, or install llama.cpp from https://llama.app",
                file=stdout,
            )
        print(f"  5. Healthcheck       mobyai --config {cfg} doctor", file=stdout)
        print(f"  6. Start             mobyai --config {cfg} start", file=stdout)
        print(
            "     (start reuses a live /v1/models endpoint; otherwise it spawns llama serve)",
            file=stdout,
        )
    print(
        f"  7. Create a project  mobyai --config {cfg} init --name demo --objective \"...\"",
        file=stdout,
    )
    print(f"  8. Run it           mobyai --config {cfg} run demo", file=stdout)
    print("", file=stdout)
    print("Full walkthrough: docs/setup.md", file=stdout)


def _launch_block(mode: str, llama_binary: str | None = None, *, ctx: int = 32768) -> str:
    if mode != ServiceMode.MANAGED.value:
        return ""
    launch_bin = llama_binary if llama_binary else "llama"
    launch_bin_yaml = _yaml_scalar(launch_bin)
    return f"""
    inference:
      max_model_len: {ctx}
    launch:
      - {launch_bin_yaml}
      - serve
      - -hf
      - "{{model}}"
      - --host
      - 127.0.0.1
      - --port
      - "{{port}}"
      - --ctx-size
      - "{{ctx}}"
      - --flash-attn
      - "on"
      - --cache-type-k
      - q8_0
      - --cache-type-v
      - q8_0
      - --parallel
      - "1"
      - -ngl
      - "99"
      - --jinja
"""


def _yaml_scalar(value: str) -> str:
    if value == "":
        return '""'
    special = value[0] in ":-{}[]!#&*?|>%@`'\"" or ": " in value or ":" in value or "\n" in value
    if special or value.lower() in {"true", "false", "null", "yes", "no"}:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _yaml_device(value: str) -> str:
    stripped = value.strip()
    if stripped.lstrip("+-").isdigit():
        return stripped
    return _yaml_scalar(stripped)


def _yaml_gpu(value: str) -> str:
    stripped = value.strip()
    if stripped.lstrip("+-").isdigit():
        return stripped
    return "null"

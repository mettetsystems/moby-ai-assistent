"""First-run setup wizard. Writes a commented YAML config; does not start models."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from mobyai.inference.huggingface import (
    mask_token,
    token_from_file,
    write_token_file,
)
from mobyai.models.config import AppConfig, ServiceMode

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
        "model": "Qwen/Qwen3-32B",
        "endpoint": "http://127.0.0.1:9802",
        "device": "0",
    },
    "orchestrator": {
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "endpoint": "http://127.0.0.1:9801",
        "device": "1",
    },
    "worker": {
        "model": "Qwen/Qwen3.5-35B-A3B",
        "endpoint": "http://127.0.0.1:9800",
        "device": "0",
    },
}

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
        interactive=interactive,
        force=force,
        stdin=stdin,
        stdout=stdout,
    )
    write_setup(answers, stdout=stdout)
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
    interactive: bool,
    force: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> SetupAnswers:
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

    print("", file=stdout)
    print("Runtime deployment", file=stdout)
    print(
        "  default  — managed vLLM, dual GPU (planner+worker on 0, orchestrator on 1),",
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
            "  managed  — MobyAI spawns vLLM with CUDA_VISIBLE_DEVICES from hardware",
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

    print("", file=stdout)
    print("Models — press Enter to keep the reference id, or type a Hugging Face repo.", file=stdout)
    print("Endpoints must match GET /v1/models on that server after it starts.", file=stdout)
    planner = _collect_role(
        "planner",
        model=planner_model,
        endpoint=planner_endpoint,
        device=planner_device,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )
    orchestrator = _collect_role(
        "orchestrator",
        model=orchestrator_model,
        endpoint=orchestrator_endpoint,
        device=orchestrator_device,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )
    worker = _collect_role(
        "worker",
        model=worker_model,
        endpoint=worker_endpoint,
        device=worker_device,
        interactive=interactive,
        stdin=stdin,
        stdout=stdout,
    )

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


def render_config_yaml(answers: SetupAnswers) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    planner_launch = _launch_block(answers.mode)
    orchestrator_launch = _launch_block(answers.mode)
    worker_launch = _launch_block(answers.mode)
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
  inference_timeout_seconds: 120
  max_worker_turns: 8

context:
  worker_token_budget: 48000
  log_tail_lines: 150

persistence:
  # Crash recovery lives here, not in chat history. `mobyai resume` reloads this file.
  database_path: {_yaml_scalar(answers.database)}

huggingface:
  # Gated Hub downloads. Setup writes the token to this file (mode 0600).
  # Do not paste the token into YAML. Override with HF_TOKEN in the environment.
  token_file: {_yaml_scalar(answers.hf_token_file)}

services:
  # external: you already started the three OpenAI-compatible servers.
  # managed:  MobyAI spawns each role's `launch` argv with CUDA_VISIBLE_DEVICES from hardware.
  mode: {answers.mode}
  health_timeout_seconds: 60
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
    engine: vllm
    model: {_yaml_scalar(answers.planner.model)}
    gpu: {_yaml_gpu(answers.planner.device)}
    endpoint: {_yaml_scalar(answers.planner.endpoint)}
    quantization: q4
    kv_cache_dtype: fp8{planner_launch}
  orchestrator:
    provider: openai_compatible
    engine: configurable
    model: {_yaml_scalar(answers.orchestrator.model)}
    gpu: {_yaml_gpu(answers.orchestrator.device)}
    endpoint: {_yaml_scalar(answers.orchestrator.endpoint)}{orchestrator_launch}
  worker:
    provider: openai_compatible
    engine: vllm
    model: {_yaml_scalar(answers.worker.model)}
    gpu: {_yaml_gpu(answers.worker.device)}
    endpoint: {_yaml_scalar(answers.worker.endpoint)}
    inference:
      tensor_parallel_size: 1
      max_model_len: 65536
      kv_cache_dtype: fp8
      gpu_memory_utilization: 0.92
      enable_chunked_prefill: true
      max_num_seqs: 16{worker_launch}
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
    model: str | None,
    endpoint: str | None,
    device: str | None,
    interactive: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> RoleAnswers:
    defaults = DEFAULT_ROLES[role]
    print("", file=stdout)
    print(f"{role.title()} role", file=stdout)
    return RoleAnswers(
        model=_value(
            model,
            f"{role} Hugging Face model id",
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
    print("MobyAI setup", file=stdout)
    print(
        "This writes a YAML config and optionally a Hugging Face token file.",
        file=stdout,
    )
    print("It does not download weights or start GPUs until you run `mobyai start`.", file=stdout)
    print(
        "Default stack: managed vLLM, dual GPU, Qwen3-32B / Qwen2.5-7B / Qwen3.5-35B-A3B.",
        file=stdout,
    )
    print("Press Enter to keep a [default], or type a custom value.", file=stdout)
    if not interactive:
        print("Non-interactive: defaults plus any flags you passed.", file=stdout)
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
    print(f"  1. Review            {cfg}", file=stdout)
    print(f"  2. Validate          mobyai --config {cfg} config validate", file=stdout)
    if answers.mode == ServiceMode.EXTERNAL.value:
        print("  3. Start your three OpenAI-compatible servers on the configured ports.", file=stdout)
        print(f"  4. Healthcheck       mobyai --config {cfg} doctor", file=stdout)
        print(f"  5. Mark ready        mobyai --config {cfg} start", file=stdout)
    else:
        print("  3. Put vLLM on PATH. First start pulls weights using the HF token file.", file=stdout)
        print(f"  4. Healthcheck       mobyai --config {cfg} doctor", file=stdout)
        print(f"  5. Start             mobyai --config {cfg} start", file=stdout)
        print("     (managed mode starts Orchestrator now; Planner/Worker swap during run)", file=stdout)
    print(
        f"  6. Create a project  mobyai --config {cfg} init --name demo --objective \"...\"",
        file=stdout,
    )
    print(f"  7. Run it           mobyai --config {cfg} run demo", file=stdout)
    print("", file=stdout)
    print("Full walkthrough: docs/setup.md", file=stdout)


def _launch_block(mode: str) -> str:
    if mode != ServiceMode.MANAGED.value:
        return ""
    return """
    launch:
      - vllm
      - serve
      - "{model}"
      - --port
      - "{port}"
"""


def _yaml_scalar(value: str) -> str:
    if value == "":
        return '""'
    special = value[0] in ":-{}[]!#&*?|>%@`'\"" or ": " in value or "\n" in value
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

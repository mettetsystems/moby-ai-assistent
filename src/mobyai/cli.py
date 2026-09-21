"""MobyAI command-line interface. The harness owns state; this is the operator surface."""

from __future__ import annotations

from mobyai.tracing import traced

import argparse
import asyncio
import re
import shutil
import sys
import webbrowser
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from mobyai import __version__
from mobyai.console.server import ConsoleService, serve_console
from mobyai.dag.graph import TaskGraph
from mobyai.engine.orchestration import OrchestrationError, run_project
from mobyai.engine.planning import PlanningError, plan_project
from mobyai.engine.recovery import ProjectNotFoundError, resume_project
from mobyai.engine.state_machine import InvalidTransitionError, ProjectStateMachine
from mobyai.inference.huggingface import resolve_huggingface_token, token_from_file
from mobyai.inference.llamacpp import find_llama_binary
from mobyai.inference.service_manager import (
    RoleUnhealthyError,
    ServiceManagerError,
    device_for_role,
)
from mobyai.models.config import AgentRole, AppConfig, ServiceMode
from mobyai.models.event import EventRecord
from mobyai.models.project import Project, ProjectState
from mobyai.models.task import TaskStatus
from mobyai.runtime import (
    Runtime,
    RuntimeFactory,
    bundled_default_config,
    default_runtime_factory,
    find_config_path,
)
from mobyai.setup import SetupError, run_setup

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CONTINUE_STATES = frozenset(
    {
        ProjectState.READY,
        ProjectState.DISPATCHING,
        ProjectState.WORKING,
        ProjectState.VALIDATING,
        ProjectState.RETRYING,
        ProjectState.ESCALATING,
        ProjectState.REPLANNING,
        ProjectState.PLANNING,
        ProjectState.PLAN_READY,
    }
)


class CliError(Exception):
    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    runtime_factory: RuntimeFactory | None = None,
    runtime: Runtime | None = None,
) -> int:
    return asyncio.run(
        async_main(
            argv,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
            runtime_factory=runtime_factory,
            runtime=runtime,
        )
    )


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    runtime_factory: RuntimeFactory | None = None,
    runtime: Runtime | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    inn = stdin or sys.stdin
    try:
        args = parse_args(argv)
        return await _dispatch(
            args,
            out,
            inn,
            runtime=runtime,
            runtime_factory=runtime_factory or default_runtime_factory,
        )
    except (CliError, SetupError) as exc:
        print(str(exc), file=err)
        return exc.code
    except KeyboardInterrupt:
        print("Interrupted.", file=err)
        return 130


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mobyai",
        description=(
            "Local-first agentic software engineering harness. "
            "First time: ./scripts/setup.sh   or   python -m mobyai setup"
        ),
    )
    parser.add_argument("--config", help="Path to YAML configuration")
    parser.add_argument(
        "--version", action="version", version=f"mobyai {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create config/database and optionally a project")
    init.add_argument("--name", help="Project name")
    prompt = init.add_mutually_exclusive_group()
    prompt.add_argument("--objective", help="Coding objective for a new project")
    prompt.add_argument("--prompt-file", help="UTF-8 text file containing the full objective")
    init.add_argument("--id", dest="project_id", help="Explicit project id")
    init.add_argument("--workspace", "--target-directory", dest="workspace", help="Project target directory on this host")

    sub.add_parser("doctor", help="Check config, database, endpoints, and GPUs")
    sub.add_parser("start", help="Healthcheck or start configured inference services")

    run = sub.add_parser("run", help="Plan if needed, then execute the project")
    run.add_argument("project")
    run.add_argument("--max-steps", type=int, default=None)
    run.add_argument("-v", "--verbose", action="count", default=0)

    report = sub.add_parser("report", help="Generate a reasoning logic report without running coding tasks")
    report.add_argument("project")

    status = sub.add_parser("status", help="Show project or list all projects")
    status.add_argument("project", nargs="?")

    pause = sub.add_parser("pause", help="Pause a running project")
    pause.add_argument("project")

    resume = sub.add_parser("resume", help="Reload from disk and continue")
    resume.add_argument("project")
    resume.add_argument("--max-steps", type=int, default=None)
    resume.add_argument("-v", "--verbose", action="count", default=0)

    stop = sub.add_parser("stop", help="Pause the project and stop managed servers")
    stop.add_argument("project")

    tasks = sub.add_parser("tasks", help="List DAG tasks")
    tasks.add_argument("project")

    logs = sub.add_parser("logs", help="Show events or a task attempt log")
    logs.add_argument("project")
    logs.add_argument("--task", help="Task id whose attempt log to print")
    logs.add_argument("--limit", type=int, default=50)

    delete = sub.add_parser("delete", help="Delete project records; preserve workspace files")
    delete.add_argument("project")
    delete.add_argument("--confirm", required=True, help="Repeat the exact project ID")
    monitor = sub.add_parser("monitor", help="Monitor a project from the terminal")
    monitor.add_argument("project")
    monitor.add_argument("-v", "--verbose", action="count", default=0)
    monitor.add_argument("--follow", "-f", action="store_true", help="Poll until execution stops; Ctrl+C exits the monitor only")
    approval = sub.add_parser("approve-timeout", help="Explicitly approve a project operation timeout above 30 minutes")
    approval.add_argument("project")
    approval.add_argument("--seconds", type=int, required=True)
    approval.add_argument("--confirm", required=True, help="Repeat the project ID")
    secrets = sub.add_parser("secrets", help="Manage local vault tokens without printing values")
    secret_commands = secrets.add_subparsers(dest="secret_command", required=True)
    secret_commands.add_parser("list", help="Show provider names and configuration status only")
    from mobyai.vault import CATALOG
    secret_set = secret_commands.add_parser("set", help="Save a token using hidden input or a file")
    secret_set.add_argument("provider", choices=sorted(CATALOG))
    secret_set.add_argument("--file", help="Read UTF-8 token from a file; use - for stdin")
    secret_remove = secret_commands.add_parser("remove", help="Remove a saved token")
    secret_remove.add_argument("provider", choices=sorted(CATALOG))
    secret_remove.add_argument("--confirm", required=True, help="Repeat the provider ID")
    sub.add_parser("models", help="Show configured models and devices")

    ui = sub.add_parser("ui", help="Settings and monitor console in the browser")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8743)
    ui.add_argument("--no-browser", action="store_true")

    config = sub.add_parser("config", help="Configuration helpers")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("validate", help="Validate the YAML configuration")

    setup = sub.add_parser(
        "setup",
        help="Walk through first-time config, install llama.cpp, pull GGUF models, and test endpoints",
    )
    setup.add_argument(
        "--output",
        "-o",
        help="Write config here (default: --config or ./config.yaml)",
    )
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use defaults and flags; do not prompt",
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file",
    )
    setup.add_argument("--mode", choices=["external", "managed"])
    setup.add_argument(
        "--preset",
        choices=["default", "external", "custom"],
        help="default=managed dual-GPU reference models; still overridable per field",
    )
    setup.add_argument("--workspace", help="project.workspace_root")
    setup.add_argument("--database", help="persistence.database_path")
    setup.add_argument(
        "--hf-token",
        help="Hugging Face token (prefer the prompt or HF_TOKEN; argv is visible in ps)",
    )
    setup.add_argument(
        "--hf-token-file",
        help="Where to store the token (default: ~/MobyAI/secrets/hf_token)",
    )
    setup.add_argument(
        "--skip-hf-token",
        action="store_true",
        help="Do not prompt for or write a Hugging Face token",
    )
    download = setup.add_mutually_exclusive_group()
    download.add_argument(
        "--download-models",
        action="store_true",
        help="Pull GGUF weights, install llama.cpp, and test endpoints (default in interactive setup)",
    )
    download.add_argument(
        "--skip-download",
        action="store_true",
        help="Do not install llama.cpp, download weights, or test endpoints (default with --non-interactive)",
    )
    setup.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Do not install llama.cpp even when provisioning models",
    )
    setup.add_argument(
        "--skip-endpoint-test",
        action="store_true",
        help="Do not spawn llama.cpp to healthcheck endpoints during setup",
    )
    setup.add_argument("--planner-model")
    setup.add_argument("--planner-endpoint")
    setup.add_argument("--planner-device")
    setup.add_argument("--orchestrator-model")
    setup.add_argument("--orchestrator-endpoint")
    setup.add_argument("--orchestrator-device")
    setup.add_argument("--worker-model")
    setup.add_argument("--worker-endpoint")
    setup.add_argument("--worker-device")
    setup.add_argument(
        "--no-directories",
        action="store_true",
        help="Do not create workspace or database parent directories",
    )

    return parser.parse_args(list(argv) if argv is not None else None)


async def _dispatch(
    args: argparse.Namespace,
    stdout: TextIO,
    stdin: TextIO,
    *,
    runtime: Runtime | None,
    runtime_factory: RuntimeFactory,
) -> int:
    if args.command == "secrets":
        from mobyai.cli_operations import manage_secrets
        return manage_secrets(args, stdout, stdin)
    if args.command == "setup":
        return _cmd_setup(args, stdout, stdin)
    if args.command == "config" and args.config_command == "validate":
        return _config_validate(args, stdout)
    if args.command == "models":
        _print_models(_load_config(args.config), stdout)
        return 0
    if args.command == "init":
        if args.prompt_file:
            try:
                with Path(args.prompt_file).expanduser().open("rb") as source:
                    raw = source.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise CliError("Prompt file exceeds 1 MiB")
                args.objective = raw.decode("utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise CliError(f"Cannot read prompt file: {exc}") from exc
            if not args.objective.strip():
                raise CliError("Prompt file must not be empty")
        return await _cmd_init(args, stdout, runtime, runtime_factory)

    session = runtime
    owns_session = session is None
    if session is None:
        session = runtime_factory(_load_config(args.config))
    try:
        await session.initialize()
        from mobyai.cli_operations import monitor, delete_project, with_monitor, approve_timeout
        handlers = {
            "approve-timeout": lambda: approve_timeout(session, args.project, args.seconds, args.confirm, stdout),
            "monitor": lambda: monitor(session, args.project, stdout, verbosity=args.verbose, follow=args.follow),
            "delete": lambda: delete_project(session, args.project, args.confirm, stdout),
            "doctor": lambda: _cmd_doctor(session, stdout),
            "start": lambda: _cmd_start(session, stdout),
            "run": lambda: _cmd_run(
                session, args.project, stdout, max_steps=args.max_steps
            ),
            "report": lambda: _cmd_report(session, args.project, stdout),
            "status": lambda: _cmd_status(session, args.project, stdout),
            "pause": lambda: _cmd_pause(session, args.project, stdout),
            "resume": lambda: _cmd_resume(
                session, args.project, stdout, max_steps=args.max_steps
            ),
            "stop": lambda: _cmd_stop(session, args.project, stdout),
            "tasks": lambda: _cmd_tasks(session, args.project, stdout),
            "logs": lambda: _cmd_logs(
                session, args.project, stdout, task_id=args.task, limit=args.limit
            ),
            "ui": lambda: _cmd_ui(
                session,
                stdout,
                config_path=find_config_path(args.config),
                host=args.host,
                port=args.port,
                no_browser=args.no_browser,
            ),
        }
        handler = handlers.get(args.command)
        if handler is None:
            raise CliError(f"Unknown command: {args.command}", code=2)
        if args.command in {"run", "resume"} and args.verbose:
            return await with_monitor(session, args.project, stdout, args.verbose, handler)
        return await handler()
    finally:
        if owns_session:
            await session.aclose()


def _load_config(explicit: str | None) -> AppConfig:
    try:
        return AppConfig.from_yaml(find_config_path(explicit))
    except FileNotFoundError as exc:
        raise CliError(str(exc)) from exc
    except (ValidationError, ValueError, OSError) as exc:
        raise CliError(f"Invalid configuration: {exc}") from exc


def _config_validate(args: argparse.Namespace, stdout: TextIO) -> int:
    try:
        path = find_config_path(args.config)
        AppConfig.from_yaml(path)
    except FileNotFoundError as exc:
        raise CliError(str(exc)) from exc
    except (ValidationError, ValueError, OSError) as exc:
        raise CliError(f"Invalid configuration: {exc}") from exc
    print(f"OK  {path}", file=stdout)
    return 0


def _cmd_setup(args: argparse.Namespace, stdout: TextIO, stdin: TextIO) -> int:
    output = args.output or args.config
    return run_setup(
        output=Path(output).expanduser() if output else None,
        non_interactive=args.non_interactive,
        force=args.force,
        mode=args.mode,
        workspace=args.workspace,
        database=args.database,
        planner_model=args.planner_model,
        planner_endpoint=args.planner_endpoint,
        planner_device=args.planner_device,
        orchestrator_model=args.orchestrator_model,
        orchestrator_endpoint=args.orchestrator_endpoint,
        orchestrator_device=args.orchestrator_device,
        worker_model=args.worker_model,
        worker_endpoint=args.worker_endpoint,
        worker_device=args.worker_device,
        create_directories=False if args.no_directories else None,
        hf_token=args.hf_token,
        hf_token_file=args.hf_token_file,
        skip_hf_token=args.skip_hf_token,
        preset=args.preset,
        download_models=True if args.download_models else False if args.skip_download else None,
        skip_download=args.skip_download,
        skip_runtime=args.skip_runtime,
        skip_endpoint_test=args.skip_endpoint_test,
        stdin=stdin,
        stdout=stdout,
    )


async def _cmd_init(
    args: argparse.Namespace,
    stdout: TextIO,
    runtime: Runtime | None,
    runtime_factory: RuntimeFactory,
) -> int:
    config_path = _ensure_config_file(args.config)
    config = AppConfig.from_yaml(config_path)
    session = runtime
    owns = session is None
    if session is None:
        session = runtime_factory(config)
    try:
        await session.initialize()
        session.config.project.workspace_root.mkdir(parents=True, exist_ok=True)
        print(f"config     {config_path}", file=stdout)
        print(f"database   {session.config.persistence.database_path}", file=stdout)
        print(f"workspaces {session.config.project.workspace_root}", file=stdout)
        if args.objective is None:
            print(
                "Ready. Create a project with: mobyai init --name NAME --objective TEXT",
                file=stdout,
            )
            return 0
        project = await _create_project(session, args)
        print(f"project    {project.project_id}", file=stdout)
        print(f"workspace  {project.workspace_root}", file=stdout)
        print(f"state      {project.state.value}", file=stdout)
        return 0
    finally:
        if owns:
            await session.aclose()


def _ensure_config_file(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise CliError(f"Configuration file not found: {path}")
        return path
    try:
        return find_config_path(None)
    except FileNotFoundError:
        dest = Path.home() / "MobyAI" / "config.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            bundled_default_config().read_text(encoding="utf-8"), encoding="utf-8"
        )
        return dest


async def _create_project(runtime: Runtime, args: argparse.Namespace) -> Project:
    name = (args.name or "project").strip() or "project"
    objective = str(args.objective).strip()
    if not objective:
        raise CliError("--objective must be non-empty")
    project_id = args.project_id or await _unique_id(runtime, _slugify(name))
    existing = await runtime.store.projects.get(project_id)
    if existing is not None:
        raise CliError(f"Project already exists: {project_id}")
    workspace = (
        Path(args.workspace).expanduser()
        if args.workspace
        else runtime.config.project.workspace_root / project_id
    )
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    project = Project(
        project_id=project_id,
        name=name,
        objective=objective,
        workspace_root=workspace,
        created_at=now,
        updated_at=now,
    )
    await runtime.store.projects.save(project)
    await runtime.store.events.append(
        EventRecord(
            timestamp=now,
            project_id=project_id,
            component="cli",
            event="project_created",
            payload={"name": name},
        )
    )
    return project


async def _unique_id(runtime: Runtime, base: str) -> str:
    ids = set(await runtime.store.projects.list_ids())
    if base not in ids:
        return base
    index = 2
    while f"{base}-{index}" in ids:
        index += 1
    return f"{base}-{index}"


async def _cmd_doctor(runtime: Runtime, stdout: TextIO) -> int:
    failed = False

    def report(ok: bool, label: str, detail: str) -> None:
        nonlocal failed
        if not ok:
            failed = True
        print(f"{label:<18} {'ok' if ok else 'FAIL'}  {detail}", file=stdout)

    config = runtime.config
    report(True, "config", "valid")
    root = config.project.workspace_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".mobyai-write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report(True, "workspace", str(root))
    except OSError as exc:
        report(False, "workspace", str(exc))

    try:
        await runtime.initialize()
        report(True, "database", str(config.persistence.database_path))
    except OSError as exc:
        report(False, "database", str(exc))

    for role in AgentRole:
        model = config.models.for_role(role)
        try:
            healthy = await runtime.services.healthcheck(role)
        except Exception as exc:  # noqa: BLE001
            report(False, f"{role.value} endpoint", f"{model.endpoint} ({exc})")
            continue
        report(healthy, f"{role.value} endpoint", model.endpoint)

    smi = shutil.which("nvidia-smi")
    managed = config.services.mode is ServiceMode.MANAGED
    if smi:
        report(True, "gpu", smi)
    else:
        print(
            f"{'gpu':<18} {'FAIL' if managed else 'warn'}  nvidia-smi not found",
            file=stdout,
        )
        if managed:
            failed = True

    token = resolve_huggingface_token(config)
    token_file = config.huggingface.token_file
    if token:
        source = "HF_TOKEN" if not token_from_file(token_file) else str(token_file)
        report(True, "hf token", source)
    else:
        detail = str(token_file) if token_file else "set HF_TOKEN or huggingface.token_file"
        print(f"{'hf token':<18} warn  not found ({detail})", file=stdout)

    for role in AgentRole:
        launch = config.models.for_role(role).launch
        if not launch:
            continue
        found = shutil.which(launch[0]) or shutil.which(Path(launch[0]).name)
        if not found and Path(launch[0]).name in {"llama", "llama-server"}:
            llama = find_llama_binary()
            found = str(llama) if llama else None
        if found:
            report(True, f"{role.value} launch", found)
        elif managed:
            report(False, f"{role.value} launch", f"{launch[0]} not on PATH")
        else:
            print(
                f"{role.value + ' launch':<18} warn  {launch[0]} not on PATH (external mode)",
                file=stdout,
            )
    return 1 if failed else 0


async def _cmd_start(runtime: Runtime, stdout: TextIO) -> int:
    mode = runtime.config.services.mode.value
    print(f"starting inference servers ({mode} mode)", file=stdout)
    stdout.flush()

    set_progress = getattr(runtime.services, "set_progress", None)

    def on_progress(role: AgentRole, detail: str) -> None:
        print(f"{role.value:<18} {detail}", file=stdout)
        stdout.flush()

    if set_progress is not None:
        set_progress(on_progress)
    try:
        for role in AgentRole:
            try:
                already = await runtime.services.healthcheck(role)
            except Exception:  # noqa: BLE001
                already = False
            endpoint = await runtime.services.ensure_role_available(role)
            status = "already running" if already else "started"
            print(
                f"{role.value:<18} ready  {status}  {endpoint.base_url}  device={endpoint.device}",
                file=stdout,
            )
            stdout.flush()
    except (RoleUnhealthyError, ServiceManagerError) as exc:
        raise CliError(str(exc)) from exc
    finally:
        if set_progress is not None:
            set_progress(None)
    print("all inference servers ready", file=stdout)
    stdout.flush()
    return 0


async def _cmd_ui(
    runtime: Runtime,
    stdout: TextIO,
    *,
    config_path: Path,
    host: str,
    port: int,
    no_browser: bool,
) -> int:
    loop = asyncio.get_running_loop()

    def caller(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=60)

    service = ConsoleService(runtime, config_path, caller)
    server = serve_console(service, host=host, port=port)
    bound_host, bound_port = server.server_address[:2]
    browse_host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else bound_host
    url = f"http://{browse_host}:{bound_port}/"
    print(f"console  {url}", file=stdout)
    print("Settings: temperature, top-p, top-k, endpoints. Monitor: last 100 lines per step.", file=stdout)
    stdout.flush()
    if not no_browser:
        webbrowser.open(url)
    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        await service.workloads.close()
    return 0


async def _cmd_run(
    runtime: Runtime,
    project_id: str,
    stdout: TextIO,
    *,
    max_steps: int | None,
) -> int:
    project = await _require_project(runtime, project_id)
    return await _execute_project(runtime, project, stdout, max_steps=max_steps)


async def _cmd_resume(
    runtime: Runtime,
    project_id: str,
    stdout: TextIO,
    *,
    max_steps: int | None,
) -> int:
    try:
        snapshot = await resume_project(runtime.store, project_id)
    except ProjectNotFoundError as exc:
        raise CliError(str(exc)) from exc
    project = snapshot.project
    print(
        f"loaded {project.project_id} state={project.state.value} "
        f"tasks={len(snapshot.tasks)} attempts={len(snapshot.attempts)}",
        file=stdout,
    )
    return await _execute_project(runtime, project, stdout, max_steps=max_steps)


@traced("workload")
async def _execute_project(runtime, project, stdout, *, max_steps):
    from mobyai.engine.stopping import pause_interrupted
    from mobyai.engine.power import keep_awake
    from mobyai.engine.lease import project_lease, workspace_lease, ProjectBusyError
    try:
        with project_lease(runtime.config.persistence.database_path, project.project_id), workspace_lease(runtime.config.persistence.database_path, project.workspace_root):
            project = await _require_project(runtime, project.project_id)
            if project.state == ProjectState.PAUSED:
                if (project.last_error or '').startswith('Replan budget exhausted'):
                    project = project.model_copy(update={'last_error':None})
                project = ProjectStateMachine.resume(project)
                await runtime.store.projects.save(project)
                print(f"resumed to {project.state.value}", file=stdout)
            from mobyai.engine.timeouts import TimeoutPolicy, TimeoutApprovalRequired
            from mobyai.sandbox.builder import SandboxPreparationError
            timeout_policy = await TimeoutPolicy(runtime.store, project.project_id).load()
            execution_started = datetime.now(UTC)
            track_time = project.state not in {ProjectState.COMPLETED, ProjectState.FAILED}
            if track_time:
                await runtime.store.events.append(EventRecord(
                    timestamp=execution_started, project_id=project.project_id,
                    component="runtime", event="execution_started",
                ))
            try:
                async with keep_awake(enabled=track_time and runtime.config.runtime.keep_awake, stdout=stdout):
                    from mobyai.cli_operations import run_with_pause, PauseRequested
                    try:
                        from mobyai.sandbox.policy import activate as sandbox_policy
                        from mobyai.sandbox.preparation import run_prepared
                        with timeout_policy.activate(), sandbox_policy(runtime.config.sandbox):
                            return await run_with_pause(runtime, project.project_id,
                                lambda: run_prepared(runtime, project, stdout,
                                    lambda: _execute_project_inner(runtime, project, stdout, max_steps=max_steps)),
                                started=execution_started)
                    except PauseRequested:
                        await pause_interrupted(runtime.store, project.project_id)
                        print(f"{project.project_id} PAUSED", file=stdout, flush=True)
                        return 0
            except (TimeoutApprovalRequired, SandboxPreparationError) as exc:
                current = await _require_project(runtime, project.project_id)
                await runtime.store.projects.save(current.model_copy(update={"last_error": str(exc)}))
                await pause_interrupted(runtime.store, project.project_id)
                print(f"{project.project_id} PAUSED: {exc}", file=stdout, flush=True)
                return 0
            except asyncio.CancelledError:
                await pause_interrupted(runtime.store, project.project_id)
                raise
            except Exception:
                # Unexpected configuration/tool failures must not strand a project
                # in an active state after its process has exited.
                await pause_interrupted(runtime.store, project.project_id)
                raise
            finally:
                if track_time:
                    await runtime.store.events.append(EventRecord(
                        timestamp=datetime.now(UTC), project_id=project.project_id,
                        component="runtime", event="execution_finished",
                    ))
    except ProjectBusyError as exc:
        raise CliError(str(exc)) from exc


async def _execute_project_inner(
    runtime: Runtime,
    project: Project,
    stdout: TextIO,
    *,
    max_steps: int | None,
) -> int:
    if project.state == ProjectState.COMPLETED:
        print(f"{project.project_id} already COMPLETED", file=stdout)
        return 0
    if project.state == ProjectState.FAILED:
        raise CliError(
            f"{project.project_id} is FAILED: {project.last_error or 'no last_error'}"
        )

    workspace = runtime.workspace_for(project)
    current = project
    from mobyai.review.service import mode, prepare, approved, requested_report
    review_mode = await mode(runtime.store, project)
    if review_mode and current.state in {ProjectState.NEW, ProjectState.PLANNING}:
        print('Review phase: ' + ('reasoning history' if review_mode == 'native' else 'code archaeology'), file=stdout, flush=True)
        try:
            await prepare(runtime, current)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from mobyai.review.service import event
            await event(runtime.store,current,'code_review_failed',{'error':str(exc)})
            await runtime.store.projects.save(current.model_copy(update={'last_error':'Code review: '+str(exc)}))
            from mobyai.engine.stopping import pause_interrupted
            await pause_interrupted(runtime.store,current.project_id)
            print('Review stopped: '+str(exc), file=stdout, flush=True)
            return 1
    if current.state in {ProjectState.NEW, ProjectState.PLANNING}:
        planning_round = 0
        while True:
            try:
                current, plan = await plan_project(
                    current, runtime.store, runtime.planner,
                    workspace=workspace, services=runtime.services,
                )
                break
            except PlanningError as exc:
                from mobyai.engine.planner_recovery import recover_planner
                planning_round += 1
                print(f"Planning recovery round {planning_round}: {exc}",file=stdout,flush=True)
                current = await recover_planner(current,runtime.store,exc,ProjectState.PLANNING,planning_round)
                if current.state == ProjectState.PAUSED:
                    print(f"{current.project_id} PAUSED: {current.last_error}",file=stdout)
                    return 0
        print(
            f"planned {current.project_id}: {len(plan.tasks)} task(s) -> {current.state.value}",
            file=stdout,
        )

    if review_mode and not await approved(runtime.store,current):
        from mobyai.engine.stopping import pause_interrupted
        await pause_interrupted(runtime.store,current.project_id)
        print('Plan ready for human review in Monitor. No implementation has started.',file=stdout,flush=True)
        return 0

    if current.state == ProjectState.PLAN_READY:
        current = ProjectStateMachine.transition(current, ProjectState.READY)
        await runtime.store.projects.save(current)

    if current.state not in _CONTINUE_STATES:
        raise CliError(f"{current.project_id} is {current.state.value}, cannot run")

    steps = max_steps
    from mobyai.final_pass.service import final_pass, finish_reports
    from mobyai.engine.stopping import pause_interrupted
    from mobyai.inference.base import InferenceError
    while True:
        try:
            result = await run_project(
                current,
                runtime.store,
                runtime.worker,
                runtime.orchestrator,
                workspace,
                max_task_attempts=runtime.config.runtime.max_task_attempts,
                max_steps=steps,
                token_budget=runtime.config.context.worker_token_budget,
                max_turns=runtime.config.runtime.max_worker_turns,
                command_timeout_seconds=runtime.config.runtime.command_timeout_seconds,
                planner=runtime.planner,
                max_replans=runtime.config.runtime.max_replans,
                services=runtime.services,
                prefer_import_contracts=runtime.config.context.prefer_import_contracts,
            )
        except (OrchestrationError, InferenceError) as exc:
            failed=await _require_project(runtime,project.project_id)
            failed=failed.model_copy(update={'last_error':str(exc)})
            await runtime.store.projects.save(failed)
            recovered=await final_pass(runtime,failed) if max_steps is None else None
            if recovered is not None:
                current=recovered
                continue
            raise CliError(str(exc)) from exc
        try:
            recovered=await final_pass(runtime,result.project,
                review=result.project.state == ProjectState.COMPLETED) if max_steps is None else None
        except ValueError as exc:
            # A requested review must not silently turn into a successful completion.
            failed=result.project.model_copy(update={'state':ProjectState.READY,'last_error':str(exc)})
            await runtime.store.projects.save(failed)
            await pause_interrupted(runtime.store,failed.project_id)
            print(str(exc),file=stdout,flush=True)
            return 1
        if recovered is None: break
        current=recovered
    await finish_reports(runtime,result.project)
    print(
        f"{result.project.project_id} {result.project.state.value} "
        f"decision={result.decision.value}",
        file=stdout,
    )
    await requested_report(runtime,result.project,completed=result.project.state == ProjectState.COMPLETED)
    if result.project.last_error:
        print(f"last_error {result.project.last_error}", file=stdout)
    return 1 if result.project.state is ProjectState.FAILED else 0


async def _cmd_report(runtime, project_id, stdout):
    from mobyai.engine.lease import project_lease, workspace_lease
    from mobyai.review.service import logic_report
    project = await _require_project(runtime,project_id)
    with project_lease(runtime.config.persistence.database_path,project_id), workspace_lease(runtime.config.persistence.database_path,project.workspace_root):
        await logic_report(runtime,project)
    print('Reasoning logic report saved in '+str(project.workspace_root/'mobyMinutes'),file=stdout)
    return 0


async def _cmd_status(runtime: Runtime, project_id: str | None, stdout: TextIO) -> int:
    if project_id is None:
        ids = await runtime.store.projects.list_ids()
        if not ids:
            print("No projects.", file=stdout)
            return 0
        for item_id in ids:
            project = await runtime.store.projects.get(item_id)
            if project is None:
                continue
            print(
                f"{project.project_id:<24} {project.state.value:<12} "
                f"{project.current_task_id or '-'}",
                file=stdout,
            )
        return 0

    try:
        snapshot = await resume_project(runtime.store, project_id)
    except ProjectNotFoundError as exc:
        raise CliError(str(exc)) from exc
    project = snapshot.project
    tasks = snapshot.tasks
    required = [task for task in tasks if task.status is not TaskStatus.SUPERSEDED]
    passed = [task for task in required if task.status is TaskStatus.PASSED]
    last_passed = next(
        (task.task_id for task in reversed(tasks) if task.status is TaskStatus.PASSED),
        "-",
    )
    current = next(
        (task for task in tasks if task.task_id == project.current_task_id), None
    )
    worker = runtime.config.models.worker
    device = runtime.device_for(AgentRole.WORKER)
    print(f"project          {project.project_id} ({project.name})", file=stdout)
    print(f"state            {project.state.value}", file=stdout)
    if project.paused_from is not None:
        print(f"paused_from      {project.paused_from.value}", file=stdout)
    print(f"current_task     {project.current_task_id or '-'}", file=stdout)
    print(f"progress         {len(passed)}/{len(required)} passed", file=stdout)
    print(f"last_completed   {last_passed}", file=stdout)
    print(f"last_error       {project.last_error or '-'}", file=stdout)
    print(
        f"attempt_count    {current.attempt_count if current is not None else 0}",
        file=stdout,
    )
    print(f"active_model     {worker.model} (worker)", file=stdout)
    print(f"active_gpu       {device if device is not None else '-'}", file=stdout)
    print(f"gpu_mode         {runtime.gpu_mode_for(AgentRole.WORKER)}", file=stdout)
    if snapshot.latest_checkpoint is not None:
        latest = snapshot.latest_checkpoint
        print(f"checkpoint       {latest.kind.value} {latest.checkpoint_id}", file=stdout)
    return 0


async def _cmd_pause(runtime: Runtime, project_id: str, stdout: TextIO) -> int:
    from mobyai.engine.lease import project_lease, ProjectBusyError
    from mobyai.engine.stopping import pause_interrupted
    await _require_project(runtime, project_id)
    try:
        with project_lease(runtime.config.persistence.database_path, project_id):
            paused = await pause_interrupted(runtime.store, project_id)
            if paused.state is not ProjectState.PAUSED:
                raise CliError(f"Cannot pause a {paused.state.value} project")
            origin = paused.paused_from.value if paused.paused_from else "-"
            print(f"{paused.project_id} PAUSED (from {origin})", file=stdout)
    except ProjectBusyError:
        await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC),
            project_id=project_id, component="cli", event="pause_requested"))
        print(f"{project_id}: pause requested; use monitor to confirm PAUSED", file=stdout)
    return 0


async def _cmd_stop(runtime: Runtime, project_id: str, stdout: TextIO) -> int:
    project = await _require_project(runtime, project_id)
    if ProjectStateMachine.can_pause(project.state):
        project = ProjectStateMachine.pause(project)
        await runtime.store.projects.save(project)
        print(f"{project.project_id} PAUSED", file=stdout)
    for role in AgentRole:
        await runtime.services.stop_role(role)
    print("inference services stopped", file=stdout)
    return 0


async def _cmd_tasks(runtime: Runtime, project_id: str, stdout: TextIO) -> int:
    await _require_project(runtime, project_id)
    tasks = await runtime.store.tasks.list_for_project(project_id)
    if not tasks:
        print("No tasks. Run the project to generate a plan.", file=stdout)
        return 0
    try:
        ordered = TaskGraph(tasks).tasks()
    except Exception:
        ordered = tasks
    for task in ordered:
        deps = ",".join(task.dependencies) if task.dependencies else "-"
        print(
            f"{task.task_id:<16} {task.status.value:<12} attempts={task.attempt_count} "
            f"deps={deps} {task.name}",
            file=stdout,
        )
    return 0


async def _cmd_logs(
    runtime: Runtime,
    project_id: str,
    stdout: TextIO,
    *,
    task_id: str | None,
    limit: int,
) -> int:
    project = await _require_project(runtime, project_id)
    if task_id:
        attempts = await runtime.store.attempts.list_for_task(project.project_id, task_id)
        if not attempts:
            raise CliError(f"No attempts for {task_id}")
        latest = attempts[-1]
        if latest.test_log_path:
            path = Path(latest.test_log_path)
            if path.is_file():
                print(path.read_text(encoding="utf-8"), file=stdout, end="")
                return 0
        result = latest.result.value if latest.result is not None else "-"
        print(
            f"attempt {latest.attempt_number} result={result} "
            f"exit={latest.test_exit_code}",
            file=stdout,
        )
        return 0
    events = await runtime.store.events.list_for_project(project_id, limit=limit)
    if not events:
        print("No events.", file=stdout)
        return 0
    for event in events[-limit:]:
        task = event.task_id or "-"
        print(
            f"{event.timestamp.isoformat()}  {event.component}  {event.event}  task={task}",
            file=stdout,
        )
    return 0


def _print_models(config: AppConfig, stdout: TextIO) -> None:
    print(f"services.mode  {config.services.mode.value}", file=stdout)
    for role in AgentRole:
        model = config.models.for_role(role)
        device = device_for_role(config, role)
        kind = (
            "managed"
            if model.launch and config.services.mode is ServiceMode.MANAGED
            else "external"
        )
        print(
            f"{role.value:<14} model={model.model}  endpoint={model.endpoint}  "
            f"device={device}  provider={model.provider}  {kind}",
            file=stdout,
        )


async def _require_project(runtime: Runtime, project_id: str) -> Project:
    project = await runtime.store.projects.get(project_id)
    if project is None:
        raise CliError(f"Project not found: {project_id}")
    return project


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug[:64] or "project"


if __name__ == "__main__":
    sys.exit(main())


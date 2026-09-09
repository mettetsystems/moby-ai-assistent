from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest
import yaml

from mobyai.agents.orchestrator import OrchestratorAgent
from mobyai.agents.planner import PlannerAgent
from mobyai.agents.worker import WorkerAgent
from mobyai.cli import async_main, parse_args
from mobyai.inference.fake import FakeInferenceProvider, FakeInferenceServiceManager
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.config import AgentRole, AppConfig
from mobyai.models.project import ProjectState
from mobyai.runtime import Runtime, find_config_path
from mobyai.state.memory import InMemoryStateStore
from mobyai.state.sqlite import SqliteStateStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"
PYTHON = sys.executable


def _exists_command(relative_path: str) -> str:
    code = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({relative_path!r}).is_file() else 1)"
    )
    return f"{PYTHON} -c {code!r}"


PLAN = {
    "project_name": "demo_app",
    "architecture_summary": "Tiny demo.",
    "directory_structure": ["src/"],
    "tasks": [
        {
            "task_id": "STEP_001",
            "name": "Write app",
            "description": "Create src/app.py",
            "target_files": ["src/app.py"],
            "dependencies": [],
            "acceptance_criteria": ["src/app.py exists"],
            "test_files": [],
            "test_commands": [_exists_command("src/app.py")],
        }
    ],
    "documentation_requirements": [],
}

WRITE_APP = {
    "summary": "Wrote src/app.py",
    "done": True,
    "changed_files": ["src/app.py"],
    "tool_calls": [
        {
            "name": "write_file",
            "arguments": {"path": "src/app.py", "contents": "ok\n"},
        }
    ],
}


def _write_config(tmp_path: Path) -> Path:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    payload["persistence"]["database_path"] = str(tmp_path / "mobyai.db")
    payload["project"]["workspace_root"] = str(tmp_path / "projects")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _runtime(
    tmp_path: Path,
    *,
    planner: list[str] | None = None,
    worker: list[str] | None = None,
    orchestrator: list[str] | None = None,
    sqlite: bool = False,
) -> tuple[Path, Runtime]:
    config_path = _write_config(tmp_path)
    config = AppConfig.from_yaml(config_path)
    store = SqliteStateStore(config.persistence.database_path) if sqlite else InMemoryStateStore()
    repairs = config.runtime.max_schema_repair_attempts
    runtime = Runtime(
        config,
        store,
        PlannerAgent(
            StructuredGenerator(
                FakeInferenceProvider(planner or []), max_repair_attempts=repairs
            )
        ),
        OrchestratorAgent(
            StructuredGenerator(
                FakeInferenceProvider(orchestrator or []), max_repair_attempts=repairs
            )
        ),
        WorkerAgent(
            StructuredGenerator(
                FakeInferenceProvider(worker or []), max_repair_attempts=repairs
            )
        ),
        FakeInferenceServiceManager(),
        owns_store=sqlite,
    )
    return config_path, runtime


async def _invoke(
    argv: list[str],
    *,
    config_path: Path,
    runtime: Runtime | None = None,
) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    code = await async_main(
        ["--config", str(config_path), *argv],
        stdout=stdout,
        stderr=stderr,
        runtime=runtime,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_parse_args_requires_command() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_find_config_path_explicit(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    assert find_config_path(path) == path


async def test_config_validate_ok(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    code, out, err = await _invoke(["config", "validate"], config_path=path)
    assert code == 0
    assert "OK" in out
    assert err == ""


async def test_config_validate_rejects_unknown_keys(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["nope"] = True
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    code, _out, err = await _invoke(["config", "validate"], config_path=path)
    assert code == 1
    assert "Invalid configuration" in err


async def test_models_lists_roles(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    code, out, _err = await _invoke(["models"], config_path=path)
    assert code == 0
    assert "services.mode  external" in out
    assert "planner" in out
    assert "worker" in out
    assert "orchestrator" in out


async def test_init_creates_project(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    code, out, _err = await _invoke(
        ["init", "--name", "Demo App", "--objective", "Build a tiny service."],
        config_path=config_path,
        runtime=runtime,
    )
    assert code == 0
    assert "project    demo-app" in out
    stored = await runtime.store.projects.get("demo-app")
    assert stored is not None
    assert stored.state is ProjectState.NEW
    assert stored.workspace_root.is_dir()


async def test_init_duplicate_name_suffixes_id(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    await _invoke(
        ["init", "--name", "demo", "--objective", "one"],
        config_path=config_path,
        runtime=runtime,
    )
    code, out, _err = await _invoke(
        ["init", "--name", "demo", "--objective", "two"],
        config_path=config_path,
        runtime=runtime,
    )
    assert code == 0
    assert "project    demo-2" in out


async def test_status_lists_and_details(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    await _invoke(
        ["init", "--name", "demo", "--objective", "Build it.", "--id", "demo"],
        config_path=config_path,
        runtime=runtime,
    )
    code, out, _err = await _invoke(["status"], config_path=config_path, runtime=runtime)
    assert code == 0
    assert "demo" in out
    assert "NEW" in out
    code, out, _err = await _invoke(
        ["status", "demo"], config_path=config_path, runtime=runtime
    )
    assert code == 0
    assert "state            NEW" in out
    assert "progress         0/0 passed" in out
    assert "active_model" in out
    assert "active_gpu" in out


async def test_status_unknown_project(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    await runtime.initialize()
    code, _out, err = await _invoke(
        ["status", "missing"], config_path=config_path, runtime=runtime
    )
    assert code == 1
    assert "Project not found" in err


async def test_pause_and_resume_without_running(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path, planner=[json.dumps(PLAN)], worker=[json.dumps(WRITE_APP)])
    await _invoke(
        ["init", "--name", "demo", "--objective", "Build it.", "--id", "demo"],
        config_path=config_path,
        runtime=runtime,
    )
    code, out, _err = await _invoke(
        ["pause", "demo"], config_path=config_path, runtime=runtime
    )
    assert code == 0
    assert "PAUSED (from NEW)" in out
    stored = await runtime.store.projects.get("demo")
    assert stored is not None
    assert stored.state is ProjectState.PAUSED
    assert stored.paused_from is ProjectState.NEW


async def test_tasks_before_plan(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    await _invoke(
        ["init", "--name", "demo", "--objective", "Build it.", "--id", "demo"],
        config_path=config_path,
        runtime=runtime,
    )
    code, out, _err = await _invoke(
        ["tasks", "demo"], config_path=config_path, runtime=runtime
    )
    assert code == 0
    assert "No tasks" in out


async def test_logs_show_init_event(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    await _invoke(
        ["init", "--name", "demo", "--objective", "Build it.", "--id", "demo"],
        config_path=config_path,
        runtime=runtime,
    )
    code, out, _err = await _invoke(
        ["logs", "demo"], config_path=config_path, runtime=runtime
    )
    assert code == 0
    assert "project_created" in out


async def test_doctor_external_healthy(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    code, out, _err = await _invoke(["doctor"], config_path=config_path, runtime=runtime)
    assert code == 0
    assert "config" in out
    assert "planner endpoint" in out
    assert "ok" in out


async def test_doctor_unhealthy_endpoint(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    runtime.services.healthy[AgentRole.WORKER] = False
    code, out, _err = await _invoke(["doctor"], config_path=config_path, runtime=runtime)
    assert code == 1
    assert "worker endpoint" in out
    assert "FAIL" in out


async def test_start_ensures_external_roles(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    code, out, _err = await _invoke(["start"], config_path=config_path, runtime=runtime)
    assert code == 0
    assert runtime.services.ensured == [
        AgentRole.PLANNER,
        AgentRole.ORCHESTRATOR,
        AgentRole.WORKER,
    ]
    assert "already running" in out or "started" in out
    assert "ready" in out or "already running" in out
    assert "starting inference servers" in out
    assert "all inference servers ready" in out


async def test_stop_pauses_and_stops_services(tmp_path: Path) -> None:
    config_path, runtime = _runtime(tmp_path)
    await _invoke(
        ["init", "--name", "demo", "--objective", "Build it.", "--id", "demo"],
        config_path=config_path,
        runtime=runtime,
    )
    code, out, _err = await _invoke(
        ["stop", "demo"], config_path=config_path, runtime=runtime
    )
    assert code == 0
    assert "PAUSED" in out
    assert "stopped" in out
    assert runtime.services.stopped == [
        AgentRole.PLANNER,
        AgentRole.ORCHESTRATOR,
        AgentRole.WORKER,
    ]


async def test_run_plans_and_completes(tmp_path: Path) -> None:
    config_path, runtime = _runtime(
        tmp_path,
        planner=[json.dumps(PLAN)],
        worker=[json.dumps(WRITE_APP)],
        sqlite=True,
    )
    await _invoke(
        ["init", "--name", "demo", "--objective", "Build it.", "--id", "demo"],
        config_path=config_path,
        runtime=runtime,
    )
    code, out, err = await _invoke(
        ["run", "demo", "--max-steps", "5"],
        config_path=config_path,
        runtime=runtime,
    )
    assert err == ""
    assert code == 0
    assert "planned demo" in out
    assert "COMPLETED" in out
    stored = await runtime.store.projects.get("demo")
    assert stored is not None
    assert stored.state is ProjectState.COMPLETED
    workspace = tmp_path / "projects" / "demo"
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == "ok\n"
    code, out, _err = await _invoke(
        ["tasks", "demo"], config_path=config_path, runtime=runtime
    )
    assert "STEP_001" in out
    assert "passed" in out
    await runtime.aclose()


async def test_resume_from_sqlite_after_pause(tmp_path: Path) -> None:
    config_path, runtime = _runtime(
        tmp_path,
        planner=[json.dumps(PLAN)],
        worker=[json.dumps(WRITE_APP)],
        sqlite=True,
    )
    await _invoke(
        ["init", "--name", "demo", "--objective", "Build it.", "--id", "demo"],
        config_path=config_path,
        runtime=runtime,
    )
    await _invoke(["pause", "demo"], config_path=config_path, runtime=runtime)
    code, out, _err = await _invoke(
        ["resume", "demo", "--max-steps", "5"],
        config_path=config_path,
        runtime=runtime,
    )
    assert code == 0
    assert "loaded demo" in out
    assert "resumed to NEW" in out
    assert "COMPLETED" in out
    await runtime.aclose()

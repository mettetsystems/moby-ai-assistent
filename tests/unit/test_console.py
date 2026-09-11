from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from mobyai.console.server import ConsoleService, serve_console
from mobyai.console.settings import apply_role_settings, save_settings_overlay
from mobyai.console.snapshot import TAIL_LINES, build_monitor, last_lines
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.config import AppConfig, overlay_path_for
from mobyai.models.event import EventRecord
from mobyai.models.inference import ChatMessage, ModelRequest
from mobyai.models.project import Project, ProjectState
from mobyai.models.task import Task, TaskStatus
from mobyai.runtime import Runtime
from mobyai.state.memory import InMemoryStateStore
from tests.unit.test_cli import _runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"


def test_last_lines_keeps_only_tail() -> None:
    text = "\n".join(str(i) for i in range(150))
    tail = last_lines(text, 100)
    assert "49" not in tail.splitlines()[0] or tail.startswith("...")
    assert tail.endswith("149")
    assert len(tail.splitlines()) == 101  # marker + 100


def test_overlay_merges_sampling_and_endpoint(tmp_path: Path) -> None:
    src = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    main = tmp_path / "config.yaml"
    main.write_text(yaml.safe_dump(src), encoding="utf-8")
    overlay = overlay_path_for(main)
    overlay.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "worker": {
                        "endpoint": "http://127.0.0.1:1999",
                        "sampling": {"temperature": 0.4, "top_p": 0.8, "top_k": 40},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = AppConfig.from_yaml(main)
    assert config.models.worker.endpoint == "http://127.0.0.1:1999"
    assert config.models.worker.sampling.temperature == 0.4
    assert config.models.worker.sampling.top_k == 40
    assert config.models.planner.endpoint == src["models"]["planner"]["endpoint"]


def test_apply_and_save_settings(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = AppConfig.from_yaml(path)
    apply_role_settings(
        config,
        {
            "models": {
                "planner": {
                    "endpoint": "http://10.0.0.2:9",
                    "model": "custom-planner",
                    "temperature": 0.7,
                    "top_p": 0.9,
                    "top_k": 20,
                }
            }
        },
    )
    assert config.models.planner.endpoint == "http://10.0.0.2:9"
    dest = save_settings_overlay(path, config)
    assert dest == overlay_path_for(path)
    reloaded = AppConfig.from_yaml(path)
    assert reloaded.models.planner.model == "custom-planner"
    assert reloaded.models.planner.sampling.top_k == 20


async def test_generate_sends_sampling_and_live_endpoint() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            seen.append(json.loads(request.content.decode("utf-8")))
            assert str(request.url).startswith("http://127.0.0.1:7777/")
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]
                },
            )
        return httpx.Response(404)

    from pydantic import BaseModel

    class Tiny(BaseModel):
        ok: bool

    config = AppConfig.from_yaml(DEFAULT_CONFIG)
    config.models.planner.endpoint = "http://127.0.0.1:7777"
    config.models.planner.sampling.temperature = 0.55
    config.models.planner.sampling.top_p = 0.7
    config.models.planner.sampling.top_k = 13
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://127.0.0.1:1", "old", client=client)
    gen = StructuredGenerator(provider, settings=config.models.planner)
    result = await gen.generate(
        ModelRequest(messages=[ChatMessage(role="user", content="hi")]),
        Tiny,
    )
    assert result.ok is True
    assert seen[0]["temperature"] == 0.55
    assert seen[0]["top_p"] == 0.7
    assert seen[0]["top_k"] == 13
    assert provider.endpoint == "http://127.0.0.1:7777"
    await client.aclose()


async def test_monitor_opens_failed_step_full_workflow(tmp_path: Path) -> None:
    _config_path, runtime = _runtime(tmp_path, sqlite=True)
    await runtime.initialize()
    now = datetime.now(UTC)
    project = Project(
        project_id="demo",
        name="demo",
        objective="Build it",
        workspace_root=tmp_path / "ws",
        state=ProjectState.ESCALATING,
        last_error="tests failed",
        created_at=now,
        updated_at=now,
    )
    (tmp_path / "ws").mkdir()
    await runtime.store.projects.save(project)
    passed = Task(
        task_id="STEP_001",
        name="Store",
        description="db",
        status=TaskStatus.PASSED,
        attempt_count=1,
    )
    failed = Task(
        task_id="STEP_002",
        name="API",
        description="api",
        status=TaskStatus.FAILED,
        attempt_count=2,
        dependencies=["STEP_001"],
    )
    await runtime.store.tasks.save_all("demo", [passed, failed])
    log_path = tmp_path / "ws" / "fail.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(120)), encoding="utf-8")
    await runtime.store.attempts.save(
        TaskAttempt(
            project_id="demo",
            task_id="STEP_002",
            attempt_number=1,
            started_at=now,
            finished_at=now,
            result=AttemptResult.FAIL,
            test_exit_code=1,
            test_log_path="fail.log",
        )
    )
    await runtime.store.events.append(
        EventRecord(
            timestamp=now,
            project_id="demo",
            task_id="STEP_002",
            component="engine",
            event="RETRY",
            attempt=1,
        )
    )
    snapshot = await build_monitor(runtime, "demo")
    by_id = {step["task_id"]: step for step in snapshot["steps"]}
    assert by_id["STEP_002"]["failed"] is True
    assert "line 119" in by_id["STEP_002"]["workflow"]
    assert "RETRY" in by_id["STEP_002"]["workflow"]
    assert snapshot["failed"][0]["task_id"] == "STEP_002"
    await runtime.aclose()


def test_console_http_settings_round_trip(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = AppConfig.from_yaml(path)
    runtime = Runtime(config, InMemoryStateStore(), None, None, None, None, owns_store=False)  # type: ignore[arg-type]
    service = ConsoleService(runtime, path, caller=lambda coro: None)
    server = serve_console(service, host="127.0.0.1", port=0)
    port = server.server_address[1]
    try:
        got = httpx.get(f"http://127.0.0.1:{port}/api/settings", timeout=5)
        assert got.status_code == 200
        assert "planner" in got.json()["models"]
        body = got.json()
        body["models"]["worker"]["temperature"] = 0.3
        body["models"]["worker"]["endpoint"] = "http://127.0.0.1:4321"
        saved = httpx.put(
            f"http://127.0.0.1:{port}/api/settings",
            json={"models": body["models"]},
            timeout=5,
        )
        assert saved.status_code == 200
        assert config.models.worker.sampling.temperature == 0.3
        assert overlay_path_for(path).is_file()
        page = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)
        assert page.status_code == 200
        assert "Temperature" in page.text
        assert "collapsible" in page.text.lower() or "Failed steps" in page.text or "last 100" in page.text
    finally:
        server.shutdown()


def test_monitor_log_paths_stay_inside_project(tmp_path: Path) -> None:
    from mobyai.console.snapshot import read_log_text
    root = tmp_path / "project"
    root.mkdir()
    (root / "test.log").write_text("actual test output")
    (tmp_path / "secret.log").write_text("outside workspace")
    assert read_log_text("test.log", root) == "actual test output"
    assert read_log_text("../secret.log", root) == ""
    assert read_log_text("missing.log", root) == ""

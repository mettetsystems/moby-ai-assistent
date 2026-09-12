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


def test_console_import_http_validation(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    path, runtime = _runtime(tmp_path)
    service = ConsoleService(runtime, path, caller=asyncio.run)
    service.workloads.create = AsyncMock(return_value={'project_id': 'import-test', 'name': 'Test'})
    server = serve_console(service, host='127.0.0.1', port=0)
    url = f'http://127.0.0.1:{server.server_address[1]}/api/projects'
    try:
        payload = {'name': 'Test', 'prompt': '# Requirements\nBuild the API'}
        response = httpx.post(url, json=payload)
        assert response.status_code == 201
        service.workloads.create.assert_awaited_once_with(payload)
        assert httpx.post(url, json=payload, headers={'Origin': 'https://example.com'}).status_code == 403
        assert httpx.post(url, content='name=test').status_code == 415
        assert httpx.post(url, content='{', headers={'Content-Type':'application/json'}).status_code == 400
        assert service.workloads.create.await_count == 1
    finally:
        server.shutdown()
        server.server_close()


async def test_console_http_stop_requires_two_verifications(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    path, runtime = _runtime(tmp_path)
    loop = asyncio.get_running_loop()
    service = ConsoleService(runtime, path, caller=lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5))
    service.workloads.project_id = 'demo'
    service.workloads.process = SimpleNamespace(pid=12345, returncode=None)
    service.workloads._stop_current = AsyncMock()
    server = serve_console(service, host='127.0.0.1', port=0)
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        async with httpx.AsyncClient(base_url=base) as client:
            denied = await client.post('/api/workloads/stop', json={'project_id':'demo'})
            assert denied.status_code == 400
            challenge = await client.post('/api/workloads/prepare-stop', json={'project_id':'demo'})
            assert challenge.status_code == 200
            payload = {**challenge.json(), 'confirmation':'wrong'}
            assert (await client.post('/api/workloads/stop', json=payload)).status_code == 400
            service.workloads._stop_current.assert_not_called()
            payload['confirmation'] = 'demo'
            assert (await client.post('/api/workloads/stop', json=payload)).status_code == 200
            await service.workloads.stop_task
            service.workloads._stop_current.assert_awaited_once()
            assert (await client.post('/api/workloads/stop', json=payload)).status_code == 400
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


def test_saving_settings_preserves_tracing_and_recovery_profiles(tmp_path):
    path = tmp_path/'config.yaml'
    path.write_text(DEFAULT_CONFIG.read_text())
    overlay = overlay_path_for(path)
    overlay.write_text(yaml.safe_dump({'tracing': {'enabled':True}, 'models': {'worker': {'recovery_sampling':[]}}}))
    config = AppConfig.from_yaml(path)
    save_settings_overlay(path, config)
    config = AppConfig.from_yaml(path)
    assert config.tracing.enabled
    assert config.models.worker.recovery_sampling == []


def test_workload_action_http_routes(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock
    path, runtime = _runtime(tmp_path)
    service = ConsoleService(runtime, path, caller=asyncio.run)
    for action in ('resume','restart','change_target'):
        setattr(service.workloads,action,AsyncMock(return_value={'project_id':'demo'}))
    server = serve_console(service,port=0)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_address[1]}') as client:
            for route, action in [('resume','resume'),('restart','restart'),('target','change_target')]:
                payload = {'project_id':'demo','target_directory':'/tmp/example'}
                response = client.post('/api/workloads/'+route,json=payload)
                assert response.status_code == 200
                getattr(service.workloads,action).assert_awaited_once_with(payload)
                assert client.post('/api/workloads/'+route,json=payload,headers={'Origin':'https://example.com'}).status_code == 403
    finally:
        server.shutdown()
        server.server_close()


async def test_tracing_only_overlay_does_not_break_first_model_call(tmp_path):
    from mobyai.console.settings import OverlayWatcher
    from mobyai.inference.fake import FakeInferenceProvider
    from pydantic import BaseModel
    config = AppConfig.from_yaml(DEFAULT_CONFIG)
    overlay = tmp_path/'config.overlay.yaml'
    overlay.write_text('tracing:\n  enabled: true\n')
    watcher = OverlayWatcher(overlay, config)
    provider = FakeInferenceProvider(['{"ok": true}'])
    class Result(BaseModel):
        ok: bool
    baseline = config.models.planner.sampling.model_dump()
    generator = StructuredGenerator(provider, settings=config.models.planner, refresh=watcher.maybe_reload)
    result = await generator.generate(ModelRequest(messages=[ChatMessage(role='user',content='test')]), Result)
    assert result.ok
    assert len(provider.requests) == 1
    assert config.models.planner.sampling.model_dump() == baseline
    # Later model changes still apply after a tracing-only first load.
    overlay.write_text('models:\n  planner:\n    sampling:\n      temperature: 0.4\n')
    watcher._mtime = None
    watcher.maybe_reload()
    assert config.models.planner.sampling.temperature == .4


def test_model_settings_api_still_requires_models():
    import pytest
    config = AppConfig.from_yaml(DEFAULT_CONFIG)
    with pytest.raises(ValueError, match='settings.models'):
        apply_role_settings(config, {'tracing': {'enabled':True}})

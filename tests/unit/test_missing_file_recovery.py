"""Regressions for FileNotFoundError being mistaken for DNS ENOTFOUND."""
import json
import sys

import pytest

from mobyai.engine.orchestration import advance_project
from mobyai.execution.diagnostics import diagnose
from mobyai.models.project import ProjectState
from tests.unit.test_orchestrator import (
    _ready_project, _worker, _orchestrator, _retry_payload,
    _escalate_payload, _empty_payload,
)


@pytest.mark.parametrize("message", [
    "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/models/.manifest.json'",
    "E   FileNotFoundError: missing fixture directory",
])
def test_missing_file_is_local_repair(message):
    diagnosis = diagnose(message)
    assert diagnosis.category == "missing_file"
    assert diagnosis.locally_repairable
    assert not diagnosis.sampling_eligible


@pytest.mark.parametrize("message", [
    "npm ERR! EAI_AGAIN registry.npmjs.org",
    "getaddrinfo ENOTFOUND registry.npmjs.org",
    "Error: ECONNREFUSED", "ECONNRESET", "ETIMEDOUT",
    "httpx.ConnectError: connection failed", "httpx.ReadTimeout: no response",
    "FileNotFoundError: cache missing; httpx.ConnectError: download failed",
])
def test_real_network_errors_still_require_environment_repair(message):
    diagnosis = diagnose(message)
    assert diagnosis.category == "network"
    assert not diagnosis.locally_repairable
    assert not diagnosis.sampling_eligible


@pytest.mark.parametrize("message, category", [
    ("FileNotFoundError: input; Permission denied", "access"),
    ("FileNotFoundError: input; timeout approval required", "timeout_approval"),
    ("FileNotFoundError: input; context budget exhausted", "context_capacity"),
])
def test_missing_file_does_not_override_protected_blockers(message, category):
    diagnosis = diagnose(message)
    assert diagnosis.category == category
    assert not diagnosis.locally_repairable


@pytest.mark.parametrize("attempt_limit, expected", [
    (2, ProjectState.RETRYING), (1, ProjectState.ESCALATING),
])
async def test_actual_missing_directory_reaches_recovery(tmp_path, attempt_limit, expected):
    store, workspace = await _ready_project(tmp_path)
    # Real Python exception, through command execution and reduced-log routing.
    (workspace.root / "fixture_check.py").write_text(
        "from pathlib import Path\nPath('models/.manifest.json').write_text('{}')\n"
    )
    task = await store.tasks.get("proj-1", "STEP_001")
    await store.tasks.save("proj-1", task.model_copy(update={
        "test_commands": [f"{sys.executable} fixture_check.py"]
    }))
    _, worker = _worker([json.dumps(_empty_payload())])
    payload = _retry_payload() if attempt_limit == 2 else _escalate_payload()
    _, orchestrator = _orchestrator([json.dumps(payload)])
    result = await advance_project(
        await store.projects.get("proj-1"), store, worker, orchestrator, workspace,
        max_task_attempts=attempt_limit,
    )
    assert result.project.state == expected
    events = await store.events.list_for_project("proj-1")
    event = next(e for e in events if e.event == "failure_diagnosed")
    assert event.payload["category"] == "missing_file"
    assert "fixture" in event.payload["instruction"]
    assert "preserving" in event.payload["instruction"]

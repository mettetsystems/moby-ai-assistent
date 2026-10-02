"""Format drift must not discard executable work or hide the reason for recovery."""

import json

import pytest

from mobyai.engine.orchestration import advance_project
from mobyai.engine.working import execute_task
from mobyai.inference.base import SchemaRepairExhaustedError, StructuredOutputError
from mobyai.inference.fake import FakeInferenceProvider
from mobyai.inference.structured import StructuredGenerator, parse_structured
from mobyai.models.attempt import AttemptResult
from mobyai.models.config import RoleModelConfig
from mobyai.models.inference import ChatMessage, ModelRequest
from mobyai.models.worker import WorkerResult
from tests.unit.test_orchestrator import _orchestrator, _retry_payload
from tests.unit.test_worker import _agent, _ready_store, _write_app_payload


def request():
    return ModelRequest(messages=[ChatMessage(role="user", content="Continue the task.")])


def annotated_result():
    return {
        "summary": "Check the environment",
        "done": False,
        "tool_calls": [
            {"name": "list_directory", "arguments": {"path": ".venv"},
             "summary": "Verify venv exists"},
            {"name": "run_command", "arguments": {"command": ["python", "--version"]},
             "summary": "Verify package versions"},
        ],
    }


@pytest.mark.parametrize("prefix", [[], ["not JSON"]])
async def test_incidental_summaries_recover_without_another_model_call(prefix):
    payload = annotated_result()
    raw = json.dumps(payload)
    provider = FakeInferenceProvider([*prefix, raw])
    result = await StructuredGenerator(provider, max_repair_attempts=len(prefix)).generate(
        request(), WorkerResult
    )
    assert len(provider.requests) == len(prefix) + 1
    assert result.summary == payload["summary"]
    assert result.done is False
    assert [call.model_dump(mode="json") for call in result.tool_calls] == [
        {key: value for key, value in call.items() if key != "summary"}
        for call in payload["tool_calls"]
    ]
    # The canonical contract itself stays strict; tolerance is at the inference boundary.
    with pytest.raises(StructuredOutputError, match="extra_forbidden"):
        parse_structured(raw, WorkerResult)


@pytest.mark.parametrize("change", [
    {"name": "unknown_tool"},
    {"arguments": "not an object"},
    {"timeout": 600},
    {"done": True},
    {"summary": {"command": ["do", "something"]}},
    {"summary": None},
])
async def test_normalization_does_not_accept_other_contract_violations(change):
    payload = annotated_result()
    payload["tool_calls"][0].update(change)
    raw = json.dumps(payload)
    provider = FakeInferenceProvider([raw])
    with pytest.raises(SchemaRepairExhaustedError) as error:
        await StructuredGenerator(provider, max_repair_attempts=0).generate(request(), WorkerResult)
    assert error.value.raw == raw
    assert len(provider.requests) == 1


async def test_annotations_never_supply_missing_outer_summary():
    payload = annotated_result()
    del payload["summary"]
    provider = FakeInferenceProvider([json.dumps(payload)])
    with pytest.raises(SchemaRepairExhaustedError, match="Field required"):
        await StructuredGenerator(provider, max_repair_attempts=0).generate(request(), WorkerResult)


async def test_argument_data_is_not_normalized():
    payload = annotated_result()
    payload["tool_calls"][0]["arguments"]["summary"] = "Preserve argument data"
    provider = FakeInferenceProvider([json.dumps(payload)])
    result = await StructuredGenerator(provider, max_repair_attempts=0).generate(request(), WorkerResult)
    assert result.tool_calls[0].arguments == payload["tool_calls"][0]["arguments"]


@pytest.mark.parametrize("mode", [None, "json", "json_schema"])
async def test_exact_schema_is_supplied_before_first_response_in_every_mode(mode):
    settings = (RoleModelConfig(model="test", endpoint="http://localhost", structured_output=mode)
                if mode is not None else None)
    provider = FakeInferenceProvider(['{"summary":"ready"}'])
    original = ModelRequest(messages=[
        ChatMessage(role="system", content="Worker instructions"),
        *request().messages,
        ChatMessage(role="assistant", content='{"summary":"previous turn"}'),
        ChatMessage(role="user", content="Observed tool output"),
    ])
    await StructuredGenerator(provider, settings=settings).generate(original, WorkerResult)
    sent = provider.requests[0]
    schema = WorkerResult.model_json_schema()
    assert json.loads(sent.messages[0].content.split("Required JSON schema:\n", 1)[1]) == schema
    assert sent.messages[0].content.startswith("Worker instructions")
    assert "inspect_calls" not in sent.messages[0].content
    assert sent.messages[1:] == original.messages[1:]
    assert original.messages[0].content == "Worker instructions"
    assert sent.response_schema == (schema if mode == "json_schema" else None)


async def test_repair_keeps_execution_history_without_accumulating_bad_replies():
    original = ModelRequest(messages=[
        ChatMessage(role="system", content="Worker instructions"),
        *request().messages,
        ChatMessage(role="assistant", content='{"summary":"Wrote file","tool_calls":[]}'),
        ChatMessage(role="user", content="Previously executed tool output"),
    ])
    provider = FakeInferenceProvider(["first invalid reply", "second invalid reply", '{"summary":"ready"}'])
    await StructuredGenerator(provider).generate(original, WorkerResult)
    initial, first, second = provider.requests
    assert len(first.messages) == len(second.messages) == len(initial.messages) + 2
    assert second.messages[:-2] == initial.messages
    assert second.messages[-2].content == "second invalid reply"
    assert "first invalid reply" not in "\n".join(message.content for message in second.messages)


async def test_final_pass_uses_shared_schema_guidance_once():
    from mobyai.final_pass.service import FinalGenerator
    from mobyai.models.config import FinalFailConfig
    from mobyai.models.planner import PlannerOutput
    from tests.unit.test_structured import VALID_PLANNER

    provider = FakeInferenceProvider([json.dumps(VALID_PLANNER)])
    await FinalGenerator(provider, FinalFailConfig(), "Review").generate(request(), PlannerOutput)
    sent = provider.requests[0]
    assert sum(message.content.count("Required JSON schema:") for message in sent.messages) == 1
    assert json.loads(sent.messages[0].content.split("Required JSON schema:\n", 1)[1]) == PlannerOutput.model_json_schema()
    assert sent.messages[-1] == request().messages[-1]
    assert sent.json_mode is False
    assert sent.response_schema is None


async def test_invalid_batch_never_executes_its_valid_subset(tmp_path):
    store, workspace = await _ready_store(tmp_path)
    payload = _write_app_payload()
    payload["tool_calls"][0]["summary"] = "Write the implementation"
    payload["tool_calls"].append({"name": "unknown_tool", "arguments": {}, "summary": "Invalid"})
    provider, agent = _agent([json.dumps(payload)], json_repairs=0)
    result = await execute_task(await store.projects.get("proj-1"), "STEP_001", store, agent, workspace)
    assert result.attempt.result == AttemptResult.ERROR
    assert result.tool_outcomes == []
    assert not (workspace.root / "src/app.py").exists()
    assert len(provider.requests) == 1


async def test_normalized_worker_executes_once_and_records_original_evidence(tmp_path):
    store, workspace = await _ready_store(tmp_path)
    payload = _write_app_payload()
    payload["tool_calls"][0]["summary"] = "Write the implementation"
    raw = json.dumps(payload)
    provider, agent = _agent([raw], json_repairs=0)
    result = await execute_task(await store.projects.get("proj-1"), "STEP_001", store, agent, workspace)
    assert result.passed
    assert result.attempt.result == AttemptResult.PASS
    assert result.task.attempt_count == 1
    assert len(result.tool_outcomes) == len(provider.requests) == 1
    assert (workspace.root / "src/app.py").read_text() == "def run():\n    return 1\n"
    bundle = await store.records.snapshot("proj-1")
    entries = [entry for step in bundle.steps for entry in step.history if entry.kind == "schema_normalized"]
    assert len(entries) == 1
    evidence = json.loads(await store.records.read_artifact(entries[0].payload))
    assert evidence == {"schema": "WorkerResult", "removed_fields": ["tool_calls.0.summary"], "raw": raw}


async def test_schema_failure_preserves_edits_and_reaches_orchestrator_with_test_evidence(tmp_path):
    store, workspace = await _ready_store(tmp_path)
    payload = _write_app_payload()
    payload["done"] = False
    provider, worker = _agent([json.dumps(payload), "not JSON"], json_repairs=0)
    orch_provider, orchestrator = _orchestrator([json.dumps(_retry_payload())])
    step = await advance_project(await store.projects.get("proj-1"), store, worker, orchestrator, workspace)
    assert step.execution.attempt.result == AttemptResult.ERROR
    assert not step.execution.passed
    assert step.execution.changed_files == ["src/app.py"]
    assert step.execution.test_results[0].exit_code == 0
    assert (workspace.root / "src/app.py").read_text() == "def run():\n    return 1\n"
    assert len(provider.requests) == 2
    recovery_prompt = "\n".join(message.content for message in orch_provider.requests[0].messages)
    assert "Schema repair failed" in recovery_prompt
    assert "Harness validation:" in recovery_prompt
    assert "No JSON object or array found" in recovery_prompt

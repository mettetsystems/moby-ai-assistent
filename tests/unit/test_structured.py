import json

import pytest

from mobyai.inference.base import SchemaRepairExhaustedError, StructuredOutputError
from mobyai.inference.fake import FakeInferenceProvider
from mobyai.inference.structured import StructuredGenerator, parse_structured
from mobyai.models.inference import ChatMessage, ModelRequest
from mobyai.models.planner import PlannerOutput

VALID_PLANNER = {
    "project_name": "example_app",
    "architecture_summary": "Small SQLite-backed service.",
    "directory_structure": ["src/", "tests/"],
    "tasks": [
        {
            "task_id": "STEP_001",
            "name": "Implement database connection",
            "description": "Create a pooled SQLite connection layer.",
            "target_files": ["src/db.py"],
            "test_files": ["tests/test_db.py"],
            "test_commands": ["pytest tests/test_db.py"],
        }
    ],
    "documentation_requirements": ["README.md"],
}


def _request() -> ModelRequest:
    return ModelRequest(messages=[ChatMessage(role="user", content="Create an atomic plan.")])


def test_parse_plain_json() -> None:
    plan = parse_structured(json.dumps(VALID_PLANNER), PlannerOutput)
    assert plan.project_name == "example_app"
    assert plan.tasks[0].task_id == "STEP_001"


def test_parse_markdown_fenced_json() -> None:
    fenced = "```json\n" + json.dumps(VALID_PLANNER) + "\n```"
    plan = parse_structured(fenced, PlannerOutput)
    assert plan.tasks[0].name == "Implement database connection"


def test_parse_json_embedded_in_prose() -> None:
    text = "Sure, here is the plan:\n" + json.dumps(VALID_PLANNER) + "\nThanks."
    plan = parse_structured(text, PlannerOutput)
    assert len(plan.tasks) == 1


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(StructuredOutputError, match="No JSON"):
        parse_structured("this is not json", PlannerOutput)


def test_schema_mismatch_is_rejected() -> None:
    payload = dict(VALID_PLANNER)
    payload.pop("tasks")
    with pytest.raises(StructuredOutputError, match="PlannerOutput"):
        parse_structured(json.dumps(payload), PlannerOutput)


async def test_repair_succeeds_on_second_attempt() -> None:
    provider = FakeInferenceProvider(
        [
            "not json at all",
            json.dumps(VALID_PLANNER),
        ]
    )
    generator = StructuredGenerator(provider, max_repair_attempts=2)
    plan = await generator.generate(_request(), PlannerOutput)
    assert plan.project_name == "example_app"
    assert len(provider.requests) == 2
    assert "not valid structured output" in provider.requests[1].messages[-1].content.lower()


async def test_repair_exhaustion() -> None:
    provider = FakeInferenceProvider(["nope", "still nope", "also nope"])
    generator = StructuredGenerator(provider, max_repair_attempts=2)
    with pytest.raises(SchemaRepairExhaustedError) as exc:
        await generator.generate(_request(), PlannerOutput)
    assert exc.value.attempts == 2
    assert len(provider.requests) == 3

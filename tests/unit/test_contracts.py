import pytest
from pydantic import ValidationError

from mobyai.models.orchestrator import OrchestratorDecision, OrchestratorResult
from mobyai.models.planner import PlannerOutput
from mobyai.models.worker import WorkerResult, WorkerToolName


VALID_PLANNER = {
    "project_name": "example_app",
    "architecture_summary": "Small SQLite-backed service.",
    "directory_structure": ["src/", "src/main.py", "tests/"],
    "tasks": [
        {
            "task_id": "STEP_001",
            "name": "Implement database connection",
            "description": "Create a pooled SQLite connection layer.",
            "target_files": ["src/db.py"],
            "dependencies": [],
            "acceptance_criteria": ["Connection can be created."],
            "test_files": ["tests/test_db.py"],
            "test_commands": ["pytest tests/test_db.py"],
        }
    ],
    "documentation_requirements": ["README.md"],
}


def test_valid_planner_output() -> None:
    plan = PlannerOutput.model_validate(VALID_PLANNER)
    assert plan.tasks[0].task_id == "STEP_001"
    task = plan.tasks[0].to_task()
    assert task.name == "Implement database connection"
    assert task.attempt_count == 0


def test_invalid_planner_output_missing_tasks() -> None:
    payload = dict(VALID_PLANNER)
    payload.pop("tasks")
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate(payload)


def test_invalid_planner_output_extra_field() -> None:
    payload = dict(VALID_PLANNER)
    payload["secret_chain_of_thought"] = "nope"
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate(payload)


def test_valid_orchestrator_result() -> None:
    result = OrchestratorResult.model_validate(
        {
            "decision_summary": "Retry serialization.",
            "decision": "RETRY_WITH_FIX",
            "task_id": "STEP_004",
            "next_payload": {
                "target_agent": "WORKER",
                "instruction": "Fix TaskState serialization in src/state.py.",
                "required_files": ["src/state.py", "tests/test_state.py"],
            },
        }
    )
    assert result.decision == OrchestratorDecision.RETRY_WITH_FIX
    assert result.next_payload is not None
    assert result.next_payload.required_files[0] == "src/state.py"


def test_invalid_orchestrator_decision() -> None:
    with pytest.raises(ValidationError):
        OrchestratorResult.model_validate(
            {
                "decision_summary": "Invent a new action.",
                "decision": "REWRITE_EVERYTHING",
            }
        )


def test_valid_worker_result() -> None:
    result = WorkerResult.model_validate(
        {
            "summary": "Wrote src/db.py and tests.",
            "done": True,
            "changed_files": ["src/db.py", "tests/test_db.py"],
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": {"path": "src/db.py", "contents": "pass\n"},
                }
            ],
        }
    )
    assert result.tool_calls[0].name == WorkerToolName.WRITE_FILE


def test_invalid_worker_tool_name() -> None:
    with pytest.raises(ValidationError):
        WorkerResult.model_validate(
            {
                "summary": "Tried to escape the workspace.",
                "tool_calls": [{"name": "rm_rf", "arguments": {"path": "/"}}],
            }
        )

"""Scripted Planner / Worker / Orchestrator payloads for the task-manager fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
OBJECTIVE = (FIXTURE_DIR / "objective.txt").read_text(encoding="utf-8").strip()
PYTHON = sys.executable
DB_PY = (FIXTURE_DIR / "db.py").read_text(encoding="utf-8")
APP_PY = (FIXTURE_DIR / "app.py").read_text(encoding="utf-8")
README = (FIXTURE_DIR / "README.md").read_text(encoding="utf-8")


def _exists_check(relative: str) -> str:
    code = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({relative!r}).is_file() else 1)"
    )
    return f"{PYTHON} -c {code!r}"


def _write(path: str, contents: str) -> dict[str, object]:
    return {
        "summary": f"Wrote {path}",
        "done": True,
        "changed_files": [path],
        "tool_calls": [
            {"name": "write_file", "arguments": {"path": path, "contents": contents}}
        ],
    }


def empty_worker() -> dict[str, object]:
    return {
        "summary": "Claimed the REST handlers exist without writing files",
        "done": True,
        "changed_files": ["src/app.py"],
        "tool_calls": [],
    }


def plan() -> dict[str, object]:
    return {
        "project_name": "task_manager",
        "architecture_summary": (
            "Stdlib SQLite store behind a REST-shaped TaskAPI. No web framework."
        ),
        "directory_structure": ["src/", "tests/"],
        "tasks": [
            {
                "task_id": "STEP_001",
                "name": "Implement SQLite task store",
                "description": "Create src/db.py with TaskStore using sqlite3.",
                "target_files": ["src/db.py"],
                "required_context": [],
                "dependencies": [],
                "acceptance_criteria": ["TaskStore can create and list tasks in SQLite."],
                "test_files": [],
                "test_commands": [_exists_check("src/db.py")],
            },
            {
                "task_id": "STEP_002",
                "name": "Implement REST task API",
                "description": "Create src/app.py with GET/POST /tasks over TaskStore.",
                "target_files": ["src/app.py"],
                "required_context": ["src/db.py"],
                "dependencies": ["STEP_001"],
                "acceptance_criteria": ["POST /tasks then GET /tasks returns the title."],
                "test_files": [],
                "test_commands": [_exists_check("src/app.py")],
            },
            {
                "task_id": "STEP_003",
                "name": "Document the API",
                "description": "Write README.md describing the store and REST handlers.",
                "target_files": ["README.md"],
                "required_context": [],
                "dependencies": ["STEP_002"],
                "acceptance_criteria": ["README.md exists."],
                "test_files": [],
                "test_commands": [_exists_check("README.md")],
            },
        ],
        "documentation_requirements": ["README.md"],
    }


def patch() -> dict[str, object]:
    api_task = plan()["tasks"][1]
    replacement = dict(api_task)
    replacement["task_id"] = "STEP_002B"
    replacement["name"] = "Implement stdlib TaskAPI"
    replacement["description"] = (
        "Write a smaller src/app.py TaskAPI with GET/POST /tasks over TaskStore."
    )
    return {
        "summary": "Replace the failed REST task with a smaller stdlib TaskAPI.",
        "architecture_summary": None,
        "directory_structure": [],
        "supersede": [{"task_id": "STEP_002", "replacements": [replacement]}],
        "add_tasks": [],
    }


def retry_decision() -> dict[str, object]:
    return {
        "decision_summary": "src/app.py is missing after the first Worker attempt.",
        "decision": "RETRY_WITH_FIX",
        "task_id": "STEP_002",
        "next_payload": {
            "target_agent": "WORKER",
            "instruction": "Write src/app.py with TaskAPI.handle for GET/POST /tasks.",
            "required_files": ["src/app.py", "src/db.py"],
        },
    }


def escalate_decision() -> dict[str, object]:
    return {
        "decision_summary": "src/app.py is still missing after retry.",
        "decision": "ESCALATE_TO_PLANNER",
        "task_id": "STEP_002",
        "next_payload": {
            "target_agent": "PLANNER",
            "instruction": "Rescope STEP_002 into a smaller TaskAPI file-create task.",
            "required_files": ["src/app.py"],
        },
    }


def planner_responses() -> list[str]:
    return [json.dumps(plan()), json.dumps(patch())]


def worker_responses() -> list[str]:
    return [
        json.dumps(_write("src/db.py", DB_PY)),
        json.dumps(empty_worker()),
        json.dumps(empty_worker()),
        json.dumps(_write("src/app.py", APP_PY)),
        json.dumps(_write("README.md", README)),
    ]


def orchestrator_responses() -> list[str]:
    return [json.dumps(retry_decision()), json.dumps(escalate_decision())]

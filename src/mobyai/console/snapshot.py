"""Monitor snapshot: last 100 lines per step, full trace for failures."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from mobyai.dag.graph import TaskGraph
from mobyai.models.event import EventRecord
from mobyai.models.project import Project
from mobyai.models.task import Task, TaskStatus
from mobyai.runtime import Runtime

TAIL_LINES = 100


def last_lines(text: str, n: int = TAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    omitted = len(lines) - n
    return f"...[{omitted} earlier lines omitted]...\n" + "\n".join(lines[-n:])


def format_event(event: EventRecord) -> str:
    task = event.task_id or "-"
    payload = ""
    if event.payload:
        bits = [f"{key}={value}" for key, value in event.payload.items()]
        payload = "  " + " ".join(bits)
    attempt = f" attempt={event.attempt}" if event.attempt is not None else ""
    return (
        f"{event.timestamp.isoformat()}  {event.component}  {event.event}  "
        f"task={task}{attempt}{payload}"
    )


def read_log_text(path: str | None) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    return file_path.read_text(encoding="utf-8", errors="replace")


async def build_monitor(runtime: Runtime, project_id: str) -> dict[str, Any]:
    project = await runtime.store.projects.get(project_id)
    if project is None:
        raise KeyError(f"Project not found: {project_id}")
    tasks = await runtime.store.tasks.list_for_project(project_id)
    attempts = await runtime.store.attempts.list_for_project(project_id)
    events = await runtime.store.events.list_for_project(project_id)
    try:
        ordered = TaskGraph(tasks).tasks()
    except Exception:
        ordered = list(tasks)

    attempts_by_task: dict[str, list[Any]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_task[attempt.task_id].append(attempt)
    events_by_task: dict[str, list[EventRecord]] = defaultdict(list)
    unscoped: list[EventRecord] = []
    for event in events:
        if event.task_id:
            events_by_task[event.task_id].append(event)
        else:
            unscoped.append(event)

    steps = [_step_view(task, attempts_by_task[task.task_id], events_by_task[task.task_id]) for task in ordered]
    failed = [step for step in steps if step["failed"]]
    return {
        "project": _project_view(project),
        "unscoped_events": [format_event(event) for event in unscoped][-TAIL_LINES:],
        "steps": steps,
        "failed": failed,
    }


def _project_view(project: Project) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "state": project.state.value,
        "current_task_id": project.current_task_id,
        "last_error": project.last_error,
        "objective": project.objective,
    }


def _step_view(task: Task, attempts: list[Any], events: list[EventRecord]) -> dict[str, Any]:
    failed = task.status is TaskStatus.FAILED or any(
        attempt.result is not None and attempt.result.value in {"fail", "error", "timeout"}
        for attempt in attempts
    )
    event_lines = [format_event(event) for event in events]
    attempt_views = []
    combined_logs: list[str] = []
    for attempt in attempts:
        log_text = read_log_text(attempt.test_log_path)
        if log_text:
            combined_logs.append(
                f"--- attempt {attempt.attempt_number} {attempt.result} ---\n{log_text}"
            )
        elif attempt.worker_response:
            combined_logs.append(
                f"--- attempt {attempt.attempt_number} worker_response ---\n"
                f"{json.dumps(attempt.worker_response, default=str, indent=2)}"
            )
        attempt_views.append(
            {
                "attempt_number": attempt.attempt_number,
                "result": None if attempt.result is None else attempt.result.value,
                "test_exit_code": attempt.test_exit_code,
                "test_log_path": attempt.test_log_path,
                "log": log_text if failed else last_lines(log_text),
            }
        )
    trace = "\n".join(event_lines)
    logs = "\n\n".join(combined_logs)
    workflow = ""
    if event_lines:
        workflow += "Events\n" + trace
    if logs:
        workflow += ("\n\n" if workflow else "") + "Attempts\n" + logs
    return {
        "task_id": task.task_id,
        "name": task.name,
        "status": task.status.value,
        "attempt_count": task.attempt_count,
        "dependencies": list(task.dependencies),
        "superseded_by": task.superseded_by,
        "failed": failed,
        "log_tail": last_lines(workflow),
        "workflow": workflow if failed else last_lines(workflow),
        "events_tail": last_lines("\n".join(event_lines)),
        "attempts": attempt_views,
    }

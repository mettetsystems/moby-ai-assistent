"""Harness writes at attempt, retry, and escalation boundaries."""

from __future__ import annotations

import json
from datetime import datetime

from mobyai.engine.recovery_evidence import file_versions, fingerprint
from mobyai.execution.workspace import Workspace
from mobyai.memory.assembly import working_set_for
from mobyai.memory.graph import (
    link_derived,
    record_artifact_ref,
    record_claim,
    record_handoff,
    record_hypothesis,
    settle_success,
)
from mobyai.models.attempt import TaskAttempt
from mobyai.models.task import Task
from mobyai.state.repository import StateStore


def _artifact_body(attempt: TaskAttempt) -> str:
    return f"attempt={attempt.attempt_number} log={attempt.test_log_path or ''}"


async def record_attempt_memory(
    store: StateStore,
    *,
    project_id: str,
    task: Task,
    attempt: TaskAttempt,
    passed: bool,
    error: str,
    instruction: str,
    workspace: Workspace,
    now: datetime,
) -> None:
    artifact = await record_artifact_ref(
        store,
        project_id=project_id,
        task_id=task.task_id,
        name=f"artifact:{task.task_id}:{attempt.attempt_number}",
        body=_artifact_body(attempt),
        now=now,
    )
    if passed:
        outcome = await settle_success(
            store, project_id=project_id, task_id=task.task_id, now=now,
            success_summary=(
                f"{task.task_id} passed harness validation on attempt {attempt.attempt_number}.\n"
                f"Task: {task.name}\n"
                f"Validated commands: {json.dumps(task.test_commands)}\n"
                f"Target files: {json.dumps(task.target_files)}"
            ),
        )
        if outcome is not None:
            await link_derived(
                store,
                project_id=project_id,
                source_id=outcome.node_id,
                target_id=artifact.node_id,
                now=now,
            )
        return
    signature = fingerprint(error, task.test_commands)
    versions = file_versions(
        workspace, [*task.target_files, *task.test_files, *task.required_context]
    )
    body = (
        f"signature={signature}\n"
        f"instruction={instruction}\n"
        f"error={error}\n"
        f"files={json.dumps(versions, sort_keys=True)}"
    )
    hypothesis = await record_hypothesis(
        store,
        project_id=project_id,
        task_id=task.task_id,
        body=body,
        fingerprint=signature,
        now=now,
    )
    await link_derived(
        store,
        project_id=project_id,
        source_id=hypothesis.node_id,
        target_id=artifact.node_id,
        now=now,
    )
    await record_handoff(
        store,
        project_id=project_id,
        task_id=task.task_id,
        body=f"Failed attempt {attempt.attempt_number}: {error}",
        now=now,
    )


async def record_retry_memory(
    store: StateStore,
    *,
    project_id: str,
    task_id: str,
    instruction: str,
    now: datetime,
    token_budget: int = 2000,
) -> str:
    await record_handoff(
        store,
        project_id=project_id,
        task_id=task_id,
        body=instruction or "Retry the assigned task.",
        now=now,
    )
    return await working_set_for(store, project_id, task_id, token_budget=token_budget)


async def record_escalation_memory(
    store: StateStore,
    *,
    project_id: str,
    task_id: str,
    summary: str,
    instruction: str,
    now: datetime,
    failure_signature: str | None = None,
    token_budget: int = 2000,
) -> str:
    await record_handoff(
        store,
        project_id=project_id,
        task_id=task_id,
        body=summary or instruction or "Escalated to planner.",
        now=now,
    )
    if instruction or summary:
        await record_claim(
            store,
            project_id=project_id,
            task_id=task_id,
            name=f"repair:{task_id}",
            body=instruction or summary,
            now=now,
        )
    return await working_set_for(
        store,
        project_id,
        task_id,
        failure_signature=failure_signature,
        token_budget=token_budget,
    )

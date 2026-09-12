"""Persist a resumable pause after the executing coroutine has stopped."""
from datetime import UTC, datetime

from mobyai.engine.state_machine import ProjectStateMachine
from mobyai.models.attempt import AttemptResult
from mobyai.models.event import EventRecord
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus


async def pause_interrupted(store, project_id: str):
    project = await store.projects.get(project_id)
    if project is None or not ProjectStateMachine.can_pause(project.state):
        return project
    now = datetime.now(UTC)
    # An interrupted tool/test cannot be resumed halfway through. Keep its history,
    # then rerun that task from the durable workspace on explicit resume.
    if project.state in {ProjectState.WORKING, ProjectState.DISPATCHING, ProjectState.VALIDATING}:
        task = await store.tasks.get(project_id, project.current_task_id) if project.current_task_id else None
        if task is not None:
            for attempt in await store.attempts.list_for_task(project_id, task.task_id):
                if attempt.finished_at is None:
                    await store.attempts.save(attempt.model_copy(update={
                        "finished_at": now, "result": AttemptResult.ERROR,
                    }))
            await store.tasks.save(project_id, task.model_copy(update={"status": TaskStatus.READY}))
        project = project.model_copy(update={"state": ProjectState.READY, "current_task_id": None})
    elif project.state is ProjectState.REPLANNING:
        project = project.model_copy(update={"state": ProjectState.ESCALATING})
    project = ProjectStateMachine.pause(project, now=now)
    await store.projects.save(project)
    await store.events.append(EventRecord(timestamp=now, project_id=project_id,
        component="runtime", event="workload_paused", payload={"resume_from": project.paused_from.value}))
    return project

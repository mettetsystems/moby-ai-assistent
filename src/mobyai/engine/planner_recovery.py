"""Recoverable planning failures start another round; no project retry cap."""
import asyncio
from datetime import UTC, datetime
from mobyai.execution.diagnostics import diagnose
from mobyai.engine.stopping import pause_interrupted
from mobyai.models.event import EventRecord


async def _backoff(seconds):
    await asyncio.sleep(seconds)


async def recover_planner(project,store,error,state,round_number):
    current=await store.projects.get(project.project_id) or project
    current=current.model_copy(update={'state':state,'paused_from':None,
        'last_error':'Planner recovery: '+str(error),'updated_at':datetime.now(UTC)})
    await store.projects.save(current)
    diagnosis=diagnose(str(error))
    if not diagnosis.sampling_eligible and not diagnosis.locally_repairable:
        return await pause_interrupted(store,project.project_id)
    delay=min(30,2**min(round_number,5))
    await store.events.append(EventRecord(timestamp=datetime.now(UTC),project_id=project.project_id,
        component='planner',event='planner_recovery_retry',payload={'round':round_number,
        'delay_seconds':delay,'error':str(error)}))
    await _backoff(delay)
    return current

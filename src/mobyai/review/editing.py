"""Human plan edits preserve the project, files, completed tasks and audit history."""
import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256

from mobyai.agents.planner import validate_planner_output, PlanValidationError
from mobyai.dag.graph import TaskGraph
from mobyai.models.planner import PlannerOutput, PlannerTaskSpec
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.review import graph, minutes
from mobyai.review.service import mode, event, proposal_hash


async def snapshot(store, project):
    if not await mode(store, project):
        raise ValueError('Plan editing is available for existing-codebase workflows')
    events = await store.events.list_for_project(project.project_id)
    if any(e.event in {'review_plan_approved', 'review_execution_authorized'} for e in events):
        raise ValueError('Only the initial plan can be edited, before its first approval')
    if project.state not in {ProjectState.PAUSED, ProjectState.READY}:
        raise ValueError('Wait until the initial plan is awaiting approval')
    tasks = await store.tasks.list_for_project(project.project_id)
    if any(t.attempt_count or t.status not in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED} for t in tasks):
        raise ValueError('Implementation has already started; only the initial plan is editable')
    if not tasks:
        raise ValueError('Wait for the initial plan before editing')
    old = minutes.read(minutes.run_dir(project)/'proposal.json')
    plan = dict(old['plan'])
    plan.update(project_name=project.name, architecture_summary=project.architecture_summary or '',
                directory_structure=project.directory_structure,
                documentation_requirements=project.documentation_requirements,
                tasks=[{k:getattr(t,k) for k in PlannerTaskSpec.model_fields} for t in tasks])
    fingerprint = await asyncio.to_thread(graph.fingerprint, project.workspace_root)
    if fingerprint != old['source_fingerprint']:
        raise ValueError('Codebase changed since the initial review; a fresh codebase review is required')
    version = sha256(json.dumps({'project':project.model_dump(mode='json'),
        'tasks':[t.model_dump(mode='json') for t in tasks], 'fingerprint':fingerprint,
        'proposal':old},sort_keys=True).encode()).hexdigest()
    return {'plan':plan, 'version':version, 'source_fingerprint':fingerprint,
            'task_statuses':{t.task_id:t.status.value for t in tasks}}


async def save(runtime, project, payload):
    current = await snapshot(runtime.store, project)
    if payload.get('version') != current['version']:
        raise ValueError('Project or codebase changed. Reload the plan before saving')
    reason = payload.get('reason')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>4000:
        raise ValueError('Explain the plan change in 1–4000 characters')
    try:
        plan = validate_planner_output(PlannerOutput.model_validate(payload.get('plan')))
    except PlanValidationError as exc:
        raise ValueError(str(exc)) from exc
    if not plan.review_influence:
        raise ValueError('Keep notes explaining how the codebase review influences this plan')
    tasks = await runtime.store.tasks.list_for_project(project.project_id)
    previous = {t.task_id:t for t in tasks}
    incoming = {t.task_id:t for t in plan.tasks}
    if not previous.keys() <= incoming.keys():
        raise ValueError('Keep existing task IDs to preserve history; edit unfinished tasks or add new tasks')
    updated=[]
    for spec in plan.tasks:
        before=previous.get(spec.task_id)
        data=spec.model_dump()
        if before and before.status in {TaskStatus.PASSED,TaskStatus.SUPERSEDED}:
            if any(getattr(before,k)!=v for k,v in data.items()):
                raise ValueError('Completed and superseded tasks cannot be changed; add a follow-up task')
            updated.append(before)
        elif before:
            updated.append(before.model_copy(update={**data,'status':TaskStatus.PENDING}))
        else:
            updated.append(spec.to_task())
    updated=TaskGraph(updated).tasks()
    data=plan.model_dump(mode='json')
    document={'plan':data,'source_fingerprint':current['source_fingerprint'],
              'revision':proposal_hash(data,current['source_fingerprint']), 'approved':False}
    directory=minutes.run_dir(project)
    # Durable invalidation comes first: a partial write can never inherit approval.
    await event(runtime.store,project,'review_plan_proposed',{'revision':document['revision'], 'human_edit':True})
    minutes.write(directory/'plan-edits'/ (current['version']+'.json'),
                  {'before':current,'after':document,'reason':reason.strip()})
    await runtime.store.tasks.save_all(project.project_id,updated)
    project=project.model_copy(update={'name':plan.project_name,'architecture_summary':plan.architecture_summary,
        'directory_structure':plan.directory_structure,'documentation_requirements':plan.documentation_requirements,
        'state':ProjectState.READY,'paused_from':None,'current_task_id':None,'last_error':None,
        'updated_at':datetime.now(UTC)})
    await runtime.store.projects.save(project)
    minutes.write(directory/'proposal.json',document)
    await event(runtime.store,project,'review_plan_edited',{'revision':document['revision'],'reason':reason.strip()})
    return {'revision':document['revision'],'message':'Plan saved. Review and approve it before continuing.'}

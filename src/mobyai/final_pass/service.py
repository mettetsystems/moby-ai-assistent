"""Validated final planning; normal orchestration performs edits and tests."""
from mobyai.records.capture import operation, emit
import asyncio
from datetime import UTC, datetime
import json

from mobyai.agents.planner import PlannerAgent, validate_planner_output
from mobyai.context.tokenizer import estimate_tokens
from mobyai.dag.graph import TaskGraph
from mobyai.engine.planning import _existing_paths, _inspector
from mobyai.engine.lease import project_lease
from mobyai.execution.diagnostics import diagnose
from mobyai.inference.base import InferenceError
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.config import AgentRole
from mobyai.models.inference import ChatMessage
from mobyai.models.planner import PlannerTaskSpec
from mobyai.models.project import ProjectState
from mobyai.models.task import TaskStatus
from mobyai.review import minutes, graph
from mobyai.review.service import approved
from mobyai.final_pass.runtime import inference

FINAL_FAIL_PROMPT = '''You are the Final Fail planner: a last-resort software recovery architect.
Review the supplied objective, current task graph, failed attempts and recent logs.
Inspect current code before treating historical failures as current: external developers
may already have fixed them. Require safe baseline checks before any repair, preserve
uncommitted developer work, requirements and test integrity. Never bypass access,
operator stop, approval or timeout policy. Return a full plan for the orchestrator;
do not write code. Preserve every existing task ID and every passed/superseded task
exactly. Modify only unfinished tasks or add tasks with new IDs. Include concrete
rationale_summary explaining the changed repair hypothesis and verification evidence.
A formatting failure is not proof of a defect in the application's source.
The existing PlannerOutput schema and read-only inspection tools still apply.'''

FINAL_REVIEW_PROMPT = '''You are the Final Review planner: a senior software engineer reviewing
completed work against the user's original objective. Inspect current source, tests,
interfaces and recorded validation. Identify only evidence-backed remaining defects
or omissions within the approved scope; no speculative refactors or cosmetic churn.
Preserve all passed/superseded tasks exactly; add new uniquely named tasks only when
an issue merits a change. Each new task must reproduce its issue before changing code,
preserve developer edits and pass meaningful regression checks. If no changes are
needed return the unchanged task plan. Explain every proposed change and its evidence
in rationale_summary. You propose; the regular orchestrator and worker implement and
validate. Never report proposed changes as applied. Output the PlannerOutput schema.'''


async def event(runtime,project,name,payload):
    from mobyai.review.service import event as record
    await record(runtime.store,project,name,payload)


class FinalGenerator(StructuredGenerator):
    def __init__(self,provider,config,prompt):
        super().__init__(provider,max_repair_attempts=2,recovery_enabled=False)
        self.config=config
        self.prompt=prompt

    async def generate(self,request,schema):
        request=request.model_copy(update={'messages':[ChatMessage(role='system',content=self.prompt),
            *request.messages],
            'json_mode':False,'response_schema':None,'temperature':0.0,'top_k':None,'top_p':None,
            'max_tokens':self.config.max_output_tokens})
        return await super().generate(request,schema)

    async def _request(self,request):
        # Includes inspection and repair turns, not just the first request.
        if estimate_tokens('\n'.join(m.content for m in request.messages))+self.config.max_output_tokens > self.config.context_tokens:
            raise InferenceError('Final pass context capacity exceeded; no evidence was silently discarded')
        return await super()._request(request)


@operation("final_pass", "plan")
async def final_pass(runtime,project,*,review=False):
    config=runtime.config.final_fail
    if not config.enabled: return None
    history=await runtime.store.events.list_for_project(project.project_id)
    phase='final_review' if review else 'final_fail'
    if review and not any(e.event=='final_review_enabled' and e.payload.get('enabled') for e in history): return None
    if not await approved(runtime.store,project): return None
    if any(e.event==phase+'_started' for e in history):
        if review and not any(e.event=='final_review_planned' for e in history):
            raise ValueError('Final Review did not produce a valid plan. Resolve its setup error or disable Final Fail/Review in Settings before resuming.')
        return None
    if not review:
        if not project.last_error: return None
        if diagnose(project.last_error).category in {'access','network','capacity','context_capacity','timeout','timeout_approval','toolchain'}:
            return None
    tasks=await runtime.store.tasks.list_for_project(project.project_id)
    if not tasks: return None
    if review:
        project=project.model_copy(update={'state':ProjectState.READY,'last_error':'Final Review pending'})
        await runtime.store.projects.save(project)
    await event(runtime,project,phase+'_started',{'model':config.model})
    directory=minutes.run_dir(project)
    try:
        fingerprint=await asyncio.to_thread(graph.fingerprint,project.workspace_root)
        attempts=await runtime.store.attempts.list_for_project(project.project_id)
        evidence={'objective':project.objective,'architecture':project.architecture_summary,
                  'last_error':project.last_error,
                  'tasks':[t.model_dump(mode='json') for t in tasks],
                  'recent_attempts':[a.model_dump(mode='json') for a in attempts[-8:]],
                  'recent_events':[e.model_dump(mode='json') for e in history[-12:]],
                  'coverage':'Latest 8 attempts and 12 events; full history remains in the project store.'}
        # Persisted logs are untrusted evidence, bounded and read inside the workspace.
        evidence['logs']=[]
        workspace=runtime.workspace_for(project)
        for attempt in attempts[-3:]:
            path=getattr(attempt,'test_log_path',None)
            if path:
                try:
                    target=workspace.resolve(str(path))
                    with target.open('rb') as stream:
                        stream.seek(max(0,target.stat().st_size-6000))
                        evidence['logs'].append({'path':str(path),'tail':stream.read(6000).decode('utf-8',errors='replace')})
                except (OSError,ValueError): pass
        # Stop only processes owned by this runtime; external inference is untouched.
        if runtime.services:
            for role in AgentRole: await runtime.services.stop_role(role)
        with project_lease(runtime.config.persistence.database_path,'colibri-final-runtime'):
            async with asyncio.timeout(config.timeout_seconds):
                async with inference(config) as provider:
                    prompts = getattr(runtime, "prompts", None)
                    prompt = (prompts.get("final_review" if review else "final_fail")
                              if prompts else (FINAL_REVIEW_PROMPT if review else FINAL_FAIL_PROMPT))
                    planner=PlannerAgent(FinalGenerator(provider,config,prompt),max_inspect_turns=5,max_plan_repairs=2)
                    with minutes.recording(project,phase):
                        plan=await planner.create_plan(json.dumps(evidence,default=str),project_name=project.name,
                            existing_paths=await _existing_paths(workspace),inspector=_inspector(workspace))
        validate_planner_output(plan)
        before={t.task_id:t for t in tasks}; incoming={t.task_id:t for t in plan.tasks}
        if not before.keys() <= incoming.keys(): raise ValueError('Final plan removed existing task IDs')
        updated=[]
        for spec in plan.tasks:
            old=before.get(spec.task_id);data=spec.model_dump()
            if old and old.status in {TaskStatus.PASSED,TaskStatus.SUPERSEDED}:
                if any(getattr(old,k)!=v for k,v in data.items()): raise ValueError('Final plan changed completed work')
                updated.append(old)
            else:
                updated.append(old.model_copy(update={**data,'status':TaskStatus.PENDING}) if old else spec.to_task())
        updated=TaskGraph(updated).tasks()
        if await asyncio.to_thread(graph.fingerprint,project.workspace_root)!=fingerprint:
            raise ValueError('Codebase changed during final planning; preserve developer changes and review again')
        document={'phase':phase,'model':config.model,'rationale':plan.rationale_summary,
                  'plan':plan.model_dump(mode='json'),'status':'planned',
                  'note':'Proposed changes only. Normal workers and harness tests determine applied outcomes.'}
        minutes.write(directory/(phase+'.json'),document)
        minutes.write_text(directory/(phase+'.md'),'# '+phase.replace('_',' ').title()+'\n\n'+plan.rationale_summary+'\n\nStatus: proposed; not yet applied.\n')
        from mobyai.engine.stopping import pause_interrupted
        await pause_interrupted(runtime.store,project.project_id)
        await runtime.store.tasks.save_all(project.project_id,updated)
        current=project.model_copy(update={'state':ProjectState.READY,'paused_from':None,
            'current_task_id':None,'last_error':None,'architecture_summary':plan.architecture_summary,
            'directory_structure':plan.directory_structure,'documentation_requirements':plan.documentation_requirements,
            'updated_at':datetime.now(UTC)})
        await runtime.store.projects.save(current)
        await event(runtime,current,phase+'_planned',{'model':config.model,'report':str(directory/(phase+'.md'))})
        await emit("plan_applied", {"proposal": plan.model_dump(mode="json"), "tasks": [t.model_dump(mode="json") for t in updated], "phase": phase})
        return current
    except asyncio.CancelledError:
        await event(runtime,project,phase+'_cancelled',{})
        raise
    except Exception as exc:
        minutes.write(directory/(phase+'.json'),{'phase':phase,'model':config.model,'status':'failed','rationale':str(exc),'plan':{'tasks':[]},'note':'No valid final plan was handed off.'})
        await event(runtime,project,phase+'_failed',{'error':str(exc)})
        if review: raise ValueError('Final Review failed: '+str(exc)) from exc
        return None


async def finish_reports(runtime,project):
    if not runtime.config.final_fail.enabled: return
    directory=minutes.run_dir(project)
    tasks=await runtime.store.tasks.list_for_project(project.project_id)
    for phase in ('final_fail','final_review'):
        path=directory/(phase+'.json')
        if not path.is_file(): continue
        doc=minutes.read(path)
        if doc.get('status')!='failed': doc['status']=project.state.value
        doc['task_outcomes']={t.task_id:{'status':t.status.value,'attempts':t.attempt_count} for t in tasks}
        minutes.write(path,doc)
        text='# '+phase.replace('_',' ').title()+'\n\nModel: '+doc['model']+'\n\n'+doc['rationale']+'\n\nProject outcome: '+project.state.value+'\n\n'
        text+='\n'.join('## '+t['task_id']+' — '+t['name']+'\n'+t['description']+'\nChecks: '+', '.join(t['test_commands'])+'\n' for t in doc['plan']['tasks'])+'\n'
        text+='\n'.join('- '+t.task_id+': '+t.status.value+' ('+str(t.attempt_count)+' attempts)' for t in tasks)
        text+='\n\nOnly passed tasks have harness validation. See task logs and the JSON plan for exact changes and acceptance checks.\n'
        minutes.write_text(directory/(phase+'.md'),text)

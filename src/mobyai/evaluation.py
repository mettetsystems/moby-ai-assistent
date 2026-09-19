"""Small isolated worker benchmark. Scripted mode checks the harness, not model quality."""
import argparse
import asyncio
from datetime import datetime, UTC
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time

from mobyai.agents.worker import WorkerAgent
from mobyai.engine.timeouts import TimeoutPolicy
from mobyai.engine.working import execute_task
from mobyai.execution.workspace import Workspace
from mobyai.inference.fake import FakeInferenceProvider
from mobyai.inference.factory import provider_from_role_config
from mobyai.inference.structured import StructuredGenerator
from mobyai.models.config import AppConfig
from mobyai.models.project import Project, ProjectState
from mobyai.models.task import Task, TaskStatus
from mobyai.state.memory import InMemoryStateStore

TESTS = '''import unittest
from solution import inclusive_sum
class Acceptance(unittest.TestCase):
    def test_zero(self): self.assertEqual(inclusive_sum(0), 0)
    def test_one(self): self.assertEqual(inclusive_sum(1), 1)
    def test_many(self): self.assertEqual(inclusive_sum(5), 15)
'''
FIX = 'def inclusive_sum(n):\n    return sum(range(n + 1))\n'
CASES = ('repair_off_by_one', 'schema_repair', 'failed_tool_recovery')


def prompt_versions():
    result={}
    for role in ('planner','orchestrator','worker'):
        module=importlib.import_module('mobyai.agents.prompts.'+role)
        text=module.SYSTEM_PROMPT+module.USER_PROMPT
        if role=='worker':
            from mobyai.execution.tool_contracts import tool_guidance
            text+=tool_guidance()
        result[role]=sha256(text.encode()).hexdigest()
    return result


async def trial(case, *, config=None):
    started=time.monotonic();requests=0;prompt_tokens=0;completion_tokens=0;usage_available=False
    with tempfile.TemporaryDirectory(prefix='moby-eval-') as directory:
        root=Path(directory)
        (root/'solution.py').write_text('def inclusive_sum(n):\n    return sum(range(n))\n')
        (root/'test_solution.py').write_text(TESTS)
        now=datetime.now(UTC)
        project=Project(project_id='evaluation',name='evaluation',objective='Fix inclusive_sum(n) to sum integers 0 through n inclusive for nonnegative n. Do not modify test_solution.py.',workspace_root=root,state=ProjectState.READY,created_at=now,updated_at=now)
        task=Task(task_id='STEP_1',name='Fix inclusive sum',description=project.objective,
            target_files=['solution.py'],test_files=['test_solution.py'],
            acceptance_criteria=['Return correct inclusive sums for 0, 1, and 5; preserve acceptance tests'],
            test_commands=[f'{sys.executable} -m unittest test_solution.py'],status=TaskStatus.READY)
        store=InMemoryStateStore()
        await store.projects.save(project);await store.tasks.save(project.project_id,task)
        if config is None:
            responses=[]
            if case=='schema_repair':responses.append('[]')
            if case=='failed_tool_recovery':responses.append(json.dumps({'summary':'Inspect absent file','done':False,'tool_calls':[{'name':'read_file','arguments':{'path':'absent.py'}}]}))
            responses.extend([json.dumps({'summary':'Fix inclusive bound','done':False,'tool_calls':[{'name':'write_file','arguments':{'path':'solution.py','contents':FIX}}]}),json.dumps({'summary':'Request validation','done':False,'tool_calls':[{'name':'run_test','arguments':{'command':[sys.executable,'-m','unittest','test_solution.py']}}]}),json.dumps({'summary':'Validation passed','done':True,'changed_files':['solution.py']})])
            provider=FakeInferenceProvider(responses);settings=None
        else:
            settings=config.models.worker
            provider=provider_from_role_config(settings,timeout_seconds=config.runtime.inference_timeout_seconds)
        generate=provider.generate
        async def measured(request):
            nonlocal requests,prompt_tokens,completion_tokens,usage_available
            requests+=1
            response=await generate(request)
            usage_available |= response.prompt_tokens is not None or response.completion_tokens is not None
            prompt_tokens+=response.prompt_tokens or 0;completion_tokens+=response.completion_tokens or 0
            return response
        provider.generate=measured
        worker=WorkerAgent(StructuredGenerator(provider,settings=settings))
        policy=await TimeoutPolicy(store,project.project_id).load()
        try:
            with policy.activate():
                result=await execute_task(project,task.task_id,store,worker,Workspace(root),max_turns=8)
            intact=(root/'test_solution.py').read_text()==TESTS
            passed=result.passed and intact
            details={'validation_exit_codes':[r.exit_code for r in result.test_results],
                     'tool_calls':len(result.tool_outcomes),'failed_tools':sum(not t.ok for t in result.tool_outcomes),
                     'acceptance_tests_preserved':intact,'error':result.project.last_error}
        except Exception as exc:
            passed=False;details={'error':type(exc).__name__+': '+str(exc)}
        finally:
            if hasattr(provider,'aclose'):await provider.aclose()
        return {'case':case,'passed':passed,'elapsed_seconds':round(time.monotonic()-started,3),
                'requests':requests,'prompt_tokens':prompt_tokens if usage_available else None,
                'completion_tokens':completion_tokens if usage_available else None,**details}


async def evaluate(*, trials=1, config=None):
    # Fault injection is deterministic/scripted; live trials measure the repair task.
    cases=CASES if config is None else ('repair_off_by_one',)
    results=[]
    for _ in range(trials):
        for case in cases:results.append(await trial(case,config=config))
    return {'mode':'live_worker' if config else 'scripted_harness',
            'scope':'Worker repair benchmark; not a full-project or planner quality score',
            'prompt_hashes':prompt_versions(),'model':config.models.worker.model if config else 'scripted',
            'role_config':({'sampling':config.models.worker.sampling.model_dump(),
                            'inference_timeout_seconds':config.runtime.inference_timeout_seconds,
                            'structured_output':config.models.worker.structured_output,
                            'stream_responses':config.models.worker.stream_responses,
                            'refresh_worker_context':config.models.worker.refresh_worker_context,
                            'compact_worker_history':config.models.worker.compact_worker_history} if config else {}),
            'trials':results,'passed':sum(r['passed'] for r in results),'total':len(results)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Explicitly contact configured worker endpoint; no server is started')
    parser.add_argument('--config',help='Configuration required for --live')
    parser.add_argument('--trials',type=int,default=1)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.trials<1 or args.trials>100:parser.error('--trials must be 1..100')
    if args.live and not args.config:parser.error('--live requires --config')
    config=AppConfig.from_yaml(Path(args.config)) if args.live else None
    report=asyncio.run(evaluate(trials=args.trials,config=config))
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(f"{report['mode']}: {report['passed']}/{report['total']} passed; report={args.output}")
    return 0 if report['passed']==report['total'] else 1


if __name__=='__main__':
    raise SystemExit(main())

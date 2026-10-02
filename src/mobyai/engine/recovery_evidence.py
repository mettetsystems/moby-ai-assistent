"""Compact recovery evidence. Records observations, never fabricates diagnoses."""
from datetime import datetime, UTC
from hashlib import sha256
import json
import re

from mobyai.models.event import EventRecord
from mobyai.execution.errors import WorkspaceError


def normalized(text):
    return ' '.join(text.lower().split())


def fingerprint(text, commands):
    # Task replacements and timestamps should not disguise an identical failure.
    text = re.sub(r'STEP_[A-Za-z0-9_]+', '<task>', text)
    text = re.sub(r'\b\d{4}-\d{2}-\d{2}T\S+', '<time>', text)
    return sha256(json.dumps([normalized(text),commands],sort_keys=True).encode()).hexdigest()


def file_versions(workspace, paths):
    result = {}
    for name in sorted(set(paths))[:50]:
        try:
            path = workspace.resolve(name, must_exist=True)
            with path.open('rb') as stream:
                body = stream.read(2_000_001)
            result[name] = sha256(body).hexdigest() if len(body)<=2_000_000 else 'too_large_to_fingerprint'
        except (OSError, ValueError, WorkspaceError):
            result[name] = 'unavailable'
    return result


async def evidence_for(store, project_id, task, workspace, error):
    from mobyai.memory.tool_failures import recall_tool_failures
    tool_failures = await recall_tool_failures(store, project_id, task, workspace, token_budget=500)
    signature = fingerprint(error, task.test_commands)
    versions = file_versions(workspace, [*task.target_files,*task.test_files,*task.required_context])
    history = await store.events.list_for_project(project_id)
    previous = [e.payload for e in history if e.event=='recovery_evidence' and e.payload.get('failure_signature')==signature][-4:]
    return {'failure_signature':signature,'files':versions,'previous_attempts':previous,
            'tool_failures': tool_failures,
            'guidance':'Separate observed facts from hypotheses. Supply a discriminating check and its expected result. Do not repeat a disproven fix without new evidence.'}


async def record_decision(store, project_id, task, decision, evidence):
    instruction = decision.next_payload.instruction if decision.next_payload else decision.decision_summary
    repeated = any(normalized(p.get('instruction','')) == normalized(instruction[:2000])
                   and p.get('files')==evidence['files'] for p in evidence['previous_attempts'])
    if repeated and decision.next_payload is not None:
        instruction = ('Fresh inspection required before editing: the same instruction already failed with unchanged inspected files. '
                       'Read the failing test and its implementation, distinguish the symptom from its cause, '
                       'and choose a new diagnostic check before attempting another fix. Previous instruction: ' + instruction)
        decision = decision.model_copy(update={'next_payload':decision.next_payload.model_copy(update={'instruction':instruction})})
    await store.events.append(EventRecord(timestamp=datetime.now(UTC),project_id=project_id,
        task_id=task.task_id, attempt=task.attempt_count,component='recovery',event='recovery_evidence',
        payload={'failure_signature':evidence['failure_signature'],'files':evidence['files'],
                 'instruction':instruction[:2000],'repeated_instruction':repeated,
                 'validation_commands':task.test_commands,
                 'tool_failures':evidence.get('tool_failures','')}))
    return decision

"""Durable, per-operation timeout recovery with an operator-owned ceiling."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha256
import json

from mobyai.models.event import EventRecord

STEPS = (600, 750, 900, 1000, 1200, 1500, 1800)
AUTO_CEILING = 1800
_active = ContextVar('timeout_policy', default=None)


class TimeoutApprovalRequired(Exception):
    pass


def current_policy():
    return _active.get()


def operation_key(kind, identity):
    return kind + ':' + sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


class TimeoutPolicy:
    def __init__(self, store, project_id):
        self.store = store
        self.project_id = project_id
        self.ceiling = AUTO_CEILING
        self.budgets = {}
        self.exhausted = {}
        self.history = []

    async def load(self):
        events = await self.store.events.list_for_project(self.project_id)
        for event in events:
            p = event.payload
            if event.event == 'timeout_limit_approved':
                self.ceiling = max(AUTO_CEILING, int(p['seconds']))
            elif event.event == 'timeout_increased':
                self.budgets[p['key']] = p['next_seconds']
                self.history.append(p)
            elif event.event == 'timeout_approval_required':
                self.exhausted[p['key']] = p['seconds']
                self.history.append(p)
        return self

    @contextmanager
    def activate(self):
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    def budget(self, key, baseline):
        if self.exhausted.get(key, 0) >= self.ceiling:
            raise TimeoutApprovalRequired(self.approval_message(key))
        if key in self.exhausted and self.ceiling > self.exhausted[key]:
            return self.ceiling
        return min(self.ceiling, max(baseline, self.budgets.get(key, 0), STEPS[0]))

    def approval_message(self, key):
        return (f'Timeout approval required: {key} exhausted {self.ceiling} seconds. '
                'Inspect progress or split the task. A longer operation requires explicit operator approval.')

    async def timed_out(self, key, seconds, label):
        candidates = [s for s in STEPS if seconds < s <= self.ceiling]
        if self.ceiling > seconds and not candidates:
            candidates = [self.ceiling]
        payload = {'key': key, 'operation': label, 'seconds': seconds,
                   'ceiling_seconds': self.ceiling}
        if candidates:
            payload['next_seconds'] = min(candidates)
            self.budgets[key] = payload['next_seconds']
            event = 'timeout_increased'
        else:
            self.exhausted[key] = seconds
            event = 'timeout_approval_required'
        self.history.append(payload)
        project = await self.store.projects.get(self.project_id)
        await self.store.events.append(EventRecord(
            timestamp=datetime.now(UTC), project_id=self.project_id,
            task_id=project.current_task_id if project else None,
            component='recovery', event=event, payload=payload))
        if not candidates:
            raise TimeoutApprovalRequired(self.approval_message(key))
        return payload['next_seconds']

    def planner_context(self):
        return ('Timeout recovery is harness-controlled. Current per-operation ceiling: '
                f'{self.ceiling} seconds. Do not change runtime settings or bypass timeouts. '
                'Review progress and distinguish legitimate slow work from hangs, interactive prompts, '
                'or unavailable services. Split slow tasks into independently validated units where useful; '
                'preserve all acceptance tests. Sampling changes do not fix a time limit. '
                'Recent timeout evidence (operation keys persist across replacement task IDs):\n' +
                json.dumps(self.history[-12:], ensure_ascii=False))


async def approve_limit(store, database_path, project_id, seconds, confirmation, *, source):
    """Shared operator approval for CLI and UI. Approval never starts execution."""
    from mobyai.engine.lease import project_lease
    if confirmation != project_id:
        raise ValueError('Confirmation must exactly match the project ID')
    if type(seconds) is not int or seconds <= AUTO_CEILING:
        raise ValueError('Specify a whole number of seconds greater than 1800')
    with project_lease(database_path, project_id):
        project = await store.projects.get(project_id)
        if project is None:
            raise ValueError('Project not found')
        if project.state.value in {'WORKING', 'DISPATCHING', 'VALIDATING', 'PLANNING', 'REPLANNING'}:
            raise ValueError('Pause the project before approving a new timeout limit')
        await store.events.append(EventRecord(timestamp=datetime.now(UTC),
            project_id=project_id, component=source, event='timeout_limit_approved',
            payload={'seconds': seconds, 'approved_by': 'operator_' + source}))
    return {'project_id': project_id, 'ceiling_seconds': seconds, 'resumed': False}


def approval_view(events):
    ceiling = AUTO_CEILING
    latest = None
    for event in events:
        if event.event == 'timeout_limit_approved':
            ceiling = max(AUTO_CEILING, int(event.payload['seconds']))
        elif event.event == 'timeout_approval_required':
            latest = event.payload
    return {'ceiling_seconds': ceiling, 'last_exhaustion': latest,
            'approval_required': bool(latest and latest['seconds'] >= ceiling)}

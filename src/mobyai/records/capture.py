"""Best-effort research capture, isolated from authoritative execution state."""
from __future__ import annotations

import inspect
import logging
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

_active = ContextVar('research_capture', default=None)
log = logging.getLogger(__name__)


def plain(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


class Recorder:
    def __init__(self, store, project, actor, scope='run'):
        self.store, self.project, self.actor, self.scope = store, project, actor, scope
        self.step_id = None

    def failed(self, exc):
        message = f'Research capture incomplete ({type(exc).__name__}): {exc}'
        log.warning(message)
        pending = getattr(self.store, '_record_warnings', None)
        if pending is None:
            pending = {}
            self.store._record_warnings = pending
        warnings = pending.setdefault(self.project.project_id, [])
        if message not in warnings:
            warnings.append(message)

    async def start(self):
        try:
            project_id = self.project.project_id
            if await self.store.records.exists(project_id):
                return
            baseline = dict(project=plain(self.project),
                            tasks=plain(await self.store.tasks.list_for_project(project_id)),
                            attempts=plain(await self.store.attempts.list_for_project(project_id)),
                            checkpoints=plain(await self.store.checkpoints.list(project_id)),
                            legacy_minutes_path=str(self.project.workspace_root / 'mobyMinutes'))
            await self.store.records.ensure_run(self.project, baseline)
        except Exception as exc:
            self.failed(exc)

    async def emit(self, kind, payload, **kwargs):
        try:
            pending = getattr(self.store, '_record_warnings', {}).get(self.project.project_id, [])
            while pending:
                saved = await self.store.records.append(self.project.project_id, scope='run', actor='harness',
                                                       kind='capture_warning', payload={'message': pending[0]})
                if saved is None:
                    return None
                pending.pop(0)
            return await self.store.records.append(
                self.project.project_id, scope=kwargs.pop('scope', self.scope),
                kind=kind, actor=self.actor, payload=plain(payload), step_id=self.step_id, **kwargs)
        except Exception as exc:
            self.failed(exc)
            return None

    async def begin_step(self, task, attempt):
        try:
            data = await self.store.records.begin_step(self.project.project_id, task, attempt)
            self.step_id, self.scope = data['id'], 'step'
            self.segment_id = uuid4()
            await self.emit('step_segment_started', {'task': plain(task), 'attempt': plain(attempt)},
                            correlation_id=self.segment_id)
        except Exception as exc:
            self.failed(exc)


async def emit(kind, payload, **kwargs):
    recorder = _active.get()
    if recorder is not None:
        return await recorder.emit(kind, payload, **kwargs)


async def begin_step(task, attempt):
    recorder = _active.get()
    if recorder is not None:
        await recorder.begin_step(task, attempt)


async def finish_step(result):
    recorder = _active.get()
    if recorder is None:
        return
    await recorder.emit('step_finished', {
        'outcome': result.attempt.result.value, 'attempt': plain(result.attempt),
        'validation': plain(result.test_results), 'changed_files': result.changed_files,
        'missing_files': result.missing_files,
    }, correlation_id=getattr(recorder, 'segment_id', None))


def operation(actor, scope='run'):
    """Capture direct engine calls as well as calls nested in CLI/runtime flows."""
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs).arguments
            runtime = bound.get('runtime')
            store = bound.get('store') or (runtime.store if runtime is not None else None)
            project = bound.get('project')
            if store is None or project is None:
                return await fn(*args, **kwargs)
            recorder = Recorder(store, project, actor, scope)
            await recorder.start()
            token = _active.set(recorder)
            identity = uuid4()
            await recorder.emit('operation_started', {'operation': fn.__name__}, correlation_id=identity)
            try:
                result = await fn(*args, **kwargs)
                if actor == 'worker' and hasattr(result, 'attempt'):
                    await finish_step(result)
                if scope == 'plan':
                    try:
                        output = result[1] if isinstance(result, tuple) else getattr(result, 'patch', None)
                        if output is not None:
                            await recorder.emit('plan_applied', {
                                'proposal': plain(output),
                                'tasks': plain(await store.tasks.list_for_project(project.project_id)),
                                'project': plain(await store.projects.get(project.project_id)),
                            })
                    except Exception as exc:
                        recorder.failed(exc)
                await recorder.emit('operation_finished', {'operation': fn.__name__},
                                    correlation_id=identity, scope=scope)
                return result
            except BaseException as exc:
                await recorder.emit('operation_error', {'operation': fn.__name__, 'error': str(exc)},
                                    correlation_id=identity, scope=scope)
                if recorder.step_id:
                    # An exception after validation must not rewrite an observed pass.
                    try:
                        attempts = await store.attempts.list_for_project(project.project_id)
                        bundle = await store.records.snapshot(project.project_id)
                        step = next(s for s in bundle.steps if str(s.id) == str(recorder.step_id))
                        attempt = next((a for a in attempts if a.task_id == step.task_id and a.attempt_number == step.attempt_number), None)
                        outcome = attempt.result.value if attempt and attempt.result and attempt.result.value != 'in_progress' else 'interrupted'
                    except Exception:
                        outcome = 'interrupted'
                    await recorder.emit('step_interrupted', {'outcome': outcome, 'error': str(exc)},
                                        correlation_id=getattr(recorder, 'segment_id', None))
                raise
            finally:
                _active.reset(token)
        return wrapped
    return decorate


def tool_action(fn):
    @wraps(fn)
    async def wrapped(self, call, **kwargs):
        identity = uuid4()
        await emit('tool_started', {'call': plain(call)}, correlation_id=identity)
        try:
            result = await fn(self, call, **kwargs)
        except BaseException as exc:
            await emit('tool_error', {'error': str(exc)}, correlation_id=identity)
            raise
        await emit('tool_finished', {'result': plain(result)}, correlation_id=identity,
                   truncated=getattr(result, 'truncated', False))
        return result
    return wrapped


class ObservedProjects:
    def __init__(self, inner, store):
        self.inner, self.store = inner, store

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def save(self, project):
        await self.inner.save(project)  # Execution-state failures keep their existing behavior.
        recorder = Recorder(self.store, project, 'harness')
        if project.state.value == 'NEW':
            await recorder.start()
        await recorder.emit('project_state', {'state': project.state.value,
                                             'last_error': project.last_error,
                                             'current_task_id': project.current_task_id})


class ObservedEvents:
    def __init__(self, inner, store):
        self.inner, self.store = inner, store

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def append(self, event):
        result = await self.inner.append(event)
        if event.project_id:
            try:
                project = await self.store.projects.get(event.project_id)
                if project is not None:
                    scope = 'plan' if any(word in event.event for word in ('plan', 'approv', 'review_execution')) else 'run'
                    actor = 'human' if any(word in event.event for word in ('approved', 'edited', 'authorized')) else event.component
                    await Recorder(self.store, project, actor, scope).emit(
                        'event:' + event.event, plain(event), reason=event.payload.get('reason'))
            except Exception as exc:
                log.warning('Research event capture incomplete: %s', exc)
        return result


def observe_store(store):
    store.projects = ObservedProjects(store.projects, store)
    store.events = ObservedEvents(store.events, store)


def command_action(fn):
    @wraps(fn)
    async def wrapped(self, command, **kwargs):
        identity = uuid4()
        await emit('command_started', {'command': command, 'cwd': str(kwargs.get('cwd') or self.workspace.root),
                                      'requested_timeout_seconds': kwargs.get('timeout_seconds', 600)}, correlation_id=identity)
        try:
            result = await fn(self, command, **kwargs)
        except BaseException as exc:
            await emit('command_error', {'error': str(exc)}, correlation_id=identity)
            raise
        await emit('command_finished', plain(result), correlation_id=identity,
                   truncated=result.stdout_truncated or result.stderr_truncated)
        return result
    return wrapped

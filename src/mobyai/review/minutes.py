"""Durable local planning records. Payloads stay in the workspace, not remote traces."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import json
import os
from uuid import uuid4

_current = ContextVar('minutes_record', default=None)


def root(workspace):
    workspace = Path(workspace).resolve()
    path = workspace / 'mobyMinutes'
    if path.is_symlink():
        raise ValueError('mobyMinutes must not be a symlink')
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path, data):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Refusing symlinked minutes path')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex)
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or path.stat().st_size > 32_000_000:
        raise ValueError('Unsafe or oversized minutes file')
    return json.loads(path.read_text(encoding='utf-8'))


def run_dir(project):
    key = sha256(project.project_id.encode()).hexdigest()[:24]
    path = root(project.workspace_root) / 'runs' / key
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Refusing symlinked minutes directory')
    path.mkdir(parents=True, exist_ok=True)
    return path


def initialize(project, origin='greenfield'):
    base = root(project.workspace_root)
    if not (base / 'manifest.json').exists():
        write(base / 'manifest.json', {'format': 'mobyai-minutes-v1', 'origin': origin})
    path = run_dir(project) / 'prompt.json'
    if not path.exists():
        write(path, {'project_id': project.project_id, 'prompt': project.objective,
                     'created_at': datetime.now(UTC).isoformat()})


def native(workspace):
    path = Path(workspace) / 'mobyMinutes' / 'manifest.json'
    if not path.is_file():
        return False
    return read(path).get('origin') == 'greenfield'


@contextmanager
def recording(project, kind):
    if project is None:
        yield None
        return
    initialize(project)
    record = {'id': datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f') + '-' + uuid4().hex[:8],
              'kind': kind, 'project_id': project.project_id, 'prompt': project.objective,
              'started_at': datetime.now(UTC).isoformat(), 'calls': [], 'mlflow_spans': [],
              'status': 'interrupted', 'reasoning_note': 'Only model-returned reasoning is captured; no missing reasoning is reconstructed.'}
    token = _current.set(record)
    try:
        yield record
        record['status'] = 'ok'
    except BaseException as exc:
        record['status'] = 'error'
        record['error'] = str(exc)
        raise
    finally:
        _current.reset(token)
        record['finished_at'] = datetime.now(UTC).isoformat()
        record['mlflow_status'] = 'captured' if record['mlflow_spans'] else 'disabled_or_unavailable'
        write(run_dir(project) / ('plan-' + record['id'] + '.json'), record)


def capture_call(request, response):
    record = _current.get()
    if record is not None:
        record['calls'].append({'request': request.model_dump(mode='json'),
                                'response': response.model_dump(mode='json')})


def capture_span(active):
    record = _current.get()
    if record is not None and active is not None:
        try:
            record['mlflow_spans'].append(active.to_dict())
        except Exception:
            record['trace_export_warning'] = 'One or more MLflow spans could not be serialized'


def capture_result(record, result):
    if record is None:
        return
    output = result[1] if isinstance(result, tuple) else getattr(result, 'patch', None)
    if output is not None:
        record['plan'] = output.model_dump(mode='json')


def plan_records(project):
    base = root(project.workspace_root)
    records = []
    for path in sorted(base.glob('runs/*/plan-*.json')):
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ValueError('Unsafe plan history directory')
        records.append(read(path))
    return sorted(records,key=lambda record:(record.get("started_at", ""),record["id"]))


def capture_outcome(project, result):
    initialize(project)
    data={'project_id':project.project_id,'task_id':result.task.task_id,
          'attempt':result.attempt.attempt_number,'passed':result.passed,
          'timestamp':datetime.now(UTC).isoformat(),
          'tests':[test.model_dump(mode='json') for test in result.test_results],
          'last_error':result.project.last_error}
    write(run_dir(project)/('outcome-'+uuid4().hex+'.json'),data)


def write_text(path, text):
    path=Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Unsafe minutes path')
    temporary=path.with_name('.'+path.name+'.'+uuid4().hex)
    try:
        temporary.write_text(text,encoding='utf-8')
        os.chmod(temporary,0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

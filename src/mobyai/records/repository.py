"""Transactional record journal and content-addressed payload storage."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from mobyai.models.records import ArtifactRef, CaptureEntry, PlanRecord, RecordBundle, RunRecord, StepRunRecord

SCHEMA = '''
CREATE TABLE IF NOT EXISTS record_runs (
 project_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS record_steps (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
 attempt_number INTEGER NOT NULL, data TEXT NOT NULL,
 UNIQUE(project_id, task_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS record_entries (
 project_id TEXT NOT NULL, sequence INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(project_id, sequence)
);
CREATE TABLE IF NOT EXISTS record_artifacts (
 project_id TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(project_id, digest)
);
CREATE INDEX IF NOT EXISTS idx_record_artifact_digest ON record_artifacts(digest);
'''


def now():
    return datetime.now(UTC).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')


class RecordsRepository:
    def __init__(self, db, artifact_root: Path | None):
        self._db = db
        # The configured storage location may use a symlink (for example a
        # workspace alias). Pin that trusted root once; _path still rejects
        # symlinks introduced into the resolved artifact tree afterwards.
        self.artifact_root = artifact_root.resolve() if artifact_root is not None else None
        self._blobs: dict[str, bytes] = {}

    async def _run(self, function):
        # SqliteDatabase uses a worker thread. Keep its lock held until the
        # transaction completes even if the calling agent is cancelled.
        task = asyncio.create_task(self._db.run(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def exists(self, project_id):
        return await self._run(lambda conn: conn.execute(
            'SELECT 1 FROM record_runs WHERE project_id=?', (project_id,)).fetchone() is not None)

    def _path(self, digest):
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid artifact digest')
        path = self.artifact_root / digest[:2] / digest
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('Refusing symlinked record artifact path')
        return path

    def _artifact(self, conn, project_id, payload, truncated=False):
        body = encode(payload)
        digest = sha256(body).hexdigest()
        ref = ArtifactRef(path=f'artifacts/{digest[:2]}/{digest}', sha256=digest,
                          size_bytes=len(body), truncated=truncated)
        if self.artifact_root is None:
            self._blobs[digest] = body
        else:
            path = self._path(digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                temporary = path.with_name('.' + uuid4().hex)
                try:
                    with temporary.open('xb') as stream:
                        os.chmod(temporary, 0o600)
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        conn.execute('INSERT OR IGNORE INTO record_artifacts VALUES (?, ?, ?)',
                     (project_id, digest, ref.model_dump_json()))
        return ref.model_dump(mode='json')

    async def ensure_run(self, project, baseline):
        def write(conn):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT data FROM record_runs WHERE project_id=?', (project.project_id,)).fetchone()
                if row:
                    return json.loads(row[0])
                stamp, identity = now(), str(uuid4())
                partial = project.state.value != 'NEW' or bool(baseline.get('attempts')) or bool(baseline.get('tasks'))
                data = dict(id=identity, run_id=identity, project_id=project.project_id,
                            created_at=stamp, updated_at=stamp, project_created_at=project.created_at.isoformat(),
                            original_prompt=self._artifact(conn, project.project_id, {'objective': project.objective}),
                            baseline=self._artifact(conn, project.project_id, baseline),
                            state=project.state.value, plan_id=str(uuid4()),
                            completeness='partial' if partial else 'complete',
                            warnings=['Capture began after project history already existed.'] if partial else [])
                conn.execute('INSERT INTO record_runs VALUES (?, ?, ?)', (project.project_id, identity, json.dumps(data)))
                return data
        return await self._run(write)

    async def begin_step(self, project_id, task, attempt):
        def write(conn):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT data FROM record_steps WHERE project_id=? AND task_id=? AND attempt_number=?',
                                   (project_id, task.task_id, attempt.attempt_number)).fetchone()
                if row:
                    return json.loads(row[0])
                row = conn.execute('SELECT data FROM record_runs WHERE project_id=?', (project_id,)).fetchone()
                if row is None:
                    raise LookupError(f'No captured run for project {project_id}; step capture requires successful run initialization')
                run = json.loads(row[0])
                stamp = now()
                data = dict(id=str(uuid4()), run_id=run['id'], project_id=project_id,
                            created_at=stamp, updated_at=stamp, task_id=task.task_id,
                            attempt_number=attempt.attempt_number, attempt_id=attempt.id,
                            plan_revision_id=run.get('active_revision_id'),
                            task_specification=self._artifact(conn, project_id, task.model_dump(mode='json')))
                conn.execute('INSERT INTO record_steps VALUES (?, ?, ?, ?, ?)',
                             (data['id'], project_id, task.task_id, attempt.attempt_number, json.dumps(data)))
                return data
        return await self._run(write)

    async def append(self, project_id, *, scope, kind, actor, payload, step_id=None,
                     correlation_id=None, reason=None, truncated=False):
        def write(conn):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT data FROM record_runs WHERE project_id=?', (project_id,)).fetchone()
                if row is None:
                    return None
                run = json.loads(row[0])
                sequence = conn.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM record_entries WHERE project_id=?', (project_id,)).fetchone()[0]
                entry = CaptureEntry(sequence=sequence, timestamp=now(), scope=scope, kind=kind,
                                     actor=actor, step_id=step_id, correlation_id=correlation_id,
                                     parent_revision_id=run.get('active_revision_id'), reason=reason,
                                     payload=self._artifact(conn, project_id, payload, truncated))
                conn.execute('INSERT INTO record_entries VALUES (?, ?, ?)', (project_id, sequence, entry.model_dump_json()))
                run['updated_at'] = entry.timestamp.isoformat()
                if kind == 'plan_applied':
                    run['active_revision_id'] = str(entry.id)
                if kind == 'project_state':
                    run['state'] = payload['state']
                    run['final_result'] = payload['state'] if payload['state'] in {'COMPLETED', 'FAILED'} else None
                if kind == 'capture_warning':
                    run['completeness'] = 'partial'
                    run['warnings'].append(payload['message'])
                conn.execute('UPDATE record_runs SET data=? WHERE project_id=?', (json.dumps(run), project_id))
                if step_id:
                    row = conn.execute('SELECT data FROM record_steps WHERE id=?', (str(step_id),)).fetchone()
                    if row:
                        step = json.loads(row[0])
                        step['updated_at'] = entry.timestamp.isoformat()
                        if kind == 'model_request' and not step.get('initial_prompt'):
                            step['initial_prompt'] = entry.payload.model_dump(mode='json')
                        if kind in {'step_finished', 'step_interrupted'}:
                            step['outcome'] = payload['outcome']
                            step['finished_at'] = entry.timestamp.isoformat()
                        if kind == 'step_segment_started':
                            step['outcome'], step['finished_at'] = 'in_progress', None
                        conn.execute('UPDATE record_steps SET data=? WHERE id=?', (json.dumps(step), str(step_id)))
                return entry
        return await self._run(write)

    async def snapshot(self, project_id):
        def read(conn):
            with conn:
                conn.execute('BEGIN')
                row = conn.execute('SELECT data FROM record_runs WHERE project_id=?', (project_id,)).fetchone()
                if not row:
                    raise LookupError(f'No captured records for project {project_id}')
                data = json.loads(row[0])
                revision = data.pop('active_revision_id', None)
                entries = [CaptureEntry.model_validate_json(r[0]) for r in conn.execute(
                    'SELECT data FROM record_entries WHERE project_id=? ORDER BY sequence', (project_id,))]
                steps = [StepRunRecord.model_validate_json(r[0]) for r in conn.execute(
                    'SELECT data FROM record_steps WHERE project_id=? ORDER BY task_id, attempt_number', (project_id,))]
                artifacts = [ArtifactRef.model_validate_json(r[0]) for r in conn.execute(
                    'SELECT data FROM record_artifacts WHERE project_id=? ORDER BY digest', (project_id,))]
                run = RunRecord.model_validate(data)
                run.history = [e for e in entries if e.scope == 'run']
                run.step_ids = [s.id for s in steps]
                plan = PlanRecord(id=run.plan_id, run_id=run.id, project_id=project_id,
                                  created_at=run.created_at, updated_at=run.updated_at,
                                  original_prompt=run.original_prompt, active_revision_id=revision,
                                  completeness=run.completeness, warnings=list(run.warnings),
                                  history=[e for e in entries if e.scope == 'plan'])
                for step in steps:
                    step.history = [e for e in entries if e.step_id == step.id]
                    step.completeness = run.completeness
                    step.warnings = list(run.warnings)
                # Reconcile against durable execution state, within this same read
                # transaction. Never manufacture actions missing from the journal.
                if hasattr(self, 'state_store'):
                    source = self.state_store
                    project = source.projects._projects.get(project_id)
                    state = project.state.value if project else None
                    attempts = [(a.task_id, a.attempt_number, a.result.value if a.result else None)
                                for (pid, _, _), a in source.attempts._attempts.items() if pid == project_id]
                else:
                    row = conn.execute('SELECT state FROM projects WHERE project_id=?', (project_id,)).fetchone()
                    state = row[0] if row else None
                    attempts = list(conn.execute('SELECT task_id, attempt_number, result FROM task_attempts WHERE project_id=?', (project_id,)))
                if state is not None:
                    if run.state != state:
                        run.completeness = 'partial'
                        run.warnings.append('Execution state changed without a matching captured state transition.')
                    run.state = state
                    run.final_result = state if state in {'FAILED', 'COMPLETED'} else None
                by_attempt = {(s.task_id, s.attempt_number): s for s in steps}
                for task_id, number, result in attempts:
                    step = by_attempt.get((task_id, number))
                    if step is None:
                        run.completeness = 'partial'
                        run.warnings.append(f'No StepRunRecord for existing attempt {task_id}/{number}; history was not reconstructed.')
                    elif step.outcome == 'interrupted' and result == 'error':
                        # pause_interrupted closes an unfinished execution attempt
                        # as ERROR; preserve the more precise captured interruption.
                        pass
                    elif result and result != 'in_progress' and step.outcome != result:
                        step.outcome = result
                        step.completeness = run.completeness = 'partial'
                        step.warnings.append('Outcome recovered from execution state; completion capture is missing.')
                # Detect unmatched boundaries without inventing completion or replaying work.
                started = {e.correlation_id: e for e in entries if e.kind.endswith('_started') or e.kind == 'model_request'}
                closed = {e.correlation_id for e in entries if e.kind.endswith(('_finished', '_interrupted', '_error')) or e.kind == 'model_response'}
                pending = [e for key, e in started.items() if key is not None and key not in closed]
                if pending:
                    run.completeness = plan.completeness = 'unknown' if run.completeness == 'complete' else run.completeness
                    warning = f'{len(pending)} capture boundaries have no observed completion; execution may still be active or was interrupted.'
                    run.warnings.append(warning)
                    plan.warnings.append(warning)
                    for step in steps:
                        if any(e.step_id == step.id for e in pending):
                            step.completeness = 'unknown' if step.completeness == 'complete' else step.completeness
                            step.warnings.append(warning)
                if run.completeness == 'partial':
                    plan.completeness = 'partial'
                    plan.warnings = list(dict.fromkeys([*plan.warnings, *run.warnings]))
                return RecordBundle(run=run, plan=plan, steps=steps, artifacts=artifacts)
        return await self._run(read)

    async def read_artifact(self, ref):
        def read():
            body = self._blobs[ref.sha256] if self.artifact_root is None else self._path(ref.sha256).read_bytes()
            if len(body) != ref.size_bytes or sha256(body).hexdigest() != ref.sha256:
                raise ValueError(f'Artifact integrity mismatch: {ref.sha256}')
            return body
        return await asyncio.to_thread(read) if self.artifact_root else read()

    def delete_in_transaction(self, conn, project_id):
        hashes = [r[0] for r in conn.execute('SELECT digest FROM record_artifacts WHERE project_id=?', (project_id,))]
        for table in ('record_entries', 'record_steps', 'record_artifacts', 'record_runs'):
            conn.execute(f'DELETE FROM {table} WHERE project_id=?', (project_id,))
        return hashes

    async def remove_unreferenced(self, hashes):
        def cleanup(conn):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                for digest in hashes:
                    if not conn.execute('SELECT 1 FROM record_artifacts WHERE digest=?', (digest,)).fetchone():
                        if self.artifact_root is None:
                            self._blobs.pop(digest, None)
                        else:
                            self._path(digest).unlink(missing_ok=True)
        await self._run(cleanup)

    async def delete_project(self, project_id):
        def write(conn):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                return self.delete_in_transaction(conn, project_id)
        hashes = await self._run(write)
        await self.remove_unreferenced(hashes)


class _MemoryDatabase:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(SCHEMA)

    async def run(self, fn):
        return fn(self.conn)


class InMemoryRecordsRepository(RecordsRepository):
    def __init__(self):
        super().__init__(_MemoryDatabase(), None)

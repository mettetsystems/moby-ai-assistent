"""Local prompt import and console-owned CLI workloads."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import secrets
import time
import shutil
import re
from hashlib import sha256
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mobyai.models.event import EventRecord
from mobyai.models.project import Project, ProjectState
from mobyai.engine.lease import project_lease, project_is_running, workspace_lease

MAX_PROMPT_BYTES = 1_000_000


class Workloads:
    def __init__(self, runtime, config_path: Path):
        self.runtime = runtime
        self.config_path = config_path.resolve()
        self.lock = asyncio.Lock()
        self.process = None
        self.project_id = None
        self.watcher = None
        self.stop_task = None
        self.stop_challenge = None
        self.move_task = None
        self.relocation = None
        self.final_build_task = None

    async def create(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Expected a project object")
        name, prompt = payload.get("name"), payload.get("prompt")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("Project name must contain 1–120 characters")
        if not isinstance(prompt, str) or not prompt.strip() or '\x00' in prompt:
            raise ValueError("Provide a non-empty text prompt")
        if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
            raise ValueError("Prompt exceeds the 1 MB import limit")
        if not isinstance(payload.get('final_review',False),bool):
            raise ValueError('Final Review must be true or false')
        if self.runtime.config.final_fail.enabled:
            from mobyai.final_pass.runtime import status
            if status(self.runtime.config.final_fail)['state'] != 'ready':
                raise ValueError('Pre-build final inference in Settings before launching with Final Fail/Review enabled')
        if payload.get('final_review') and not self.runtime.config.final_fail.enabled:
            raise ValueError('Enable Final Fail/Review in Settings before selecting Final Review')
        async with self.lock:
            self._assert_idle()
            workflow = payload.get('workflow', 'greenfield')
            if workflow == 'existing':
                return await self._create_existing(name.strip(), prompt, payload.get('target_directory'),payload.get('final_review',False))
            if workflow != 'greenfield':
                raise ValueError('Unknown workflow')
            return await self._create(name.strip(), prompt, payload.get('target_directory'),final_review=payload.get('final_review',False))

    def _busy(self):
        return bool((self.process is not None and self.process.returncode is None)
                    or (self.stop_task and not self.stop_task.done())
                    or (self.move_task and not self.move_task.done())
                    or (self.final_build_task and not self.final_build_task.done()))

    def _assert_idle(self):
        if self._busy():
            raise ValueError("A console workload is already running. Stop it or wait for it to finish.")

    def _target(self, value, default=None):
        if value is None or value == '':
            if default is None:
                raise ValueError('Enter a target directory')
            return default.expanduser().resolve()
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ValueError('Target directory must be an absolute path or start with ~/')
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError('Target directory must be an absolute path or start with ~/')
        if path.is_symlink():
            raise ValueError('Choose a directory path, not a symbolic link')
        return path.resolve()

    async def _assert_unused_target(self, target, source_id=None):
        for project_id in await self.runtime.store.projects.list_ids():
            if project_id == source_id:
                continue
            other = await self.runtime.store.projects.get(project_id)
            if other is not None:
                root = other.workspace_root.resolve()
                if target.is_relative_to(root) or root.is_relative_to(target):
                    raise ValueError('Target overlaps another saved project workspace; choose a separate directory')

    def _prepare_target(self, path):
        if path.exists():
            if not path.is_dir() or any(path.iterdir()):
                raise ValueError('Target directory must be new or empty; existing files will not be overwritten')
        path.mkdir(parents=True, exist_ok=True)

    async def _create(self, name, prompt, target=None, restarted_from=None, final_review=False):
        project_id = 'import-' + uuid4().hex[:12]
        workspace = self._target(target, self.runtime.config.project.workspace_root / project_id)
        await self._assert_unused_target(workspace)
        self._prepare_target(workspace)
        now = datetime.now(UTC)
        project = Project(project_id=project_id, name=name, objective=prompt,
                          workspace_root=workspace, created_at=now, updated_at=now)
        await self.runtime.store.projects.save(project)
        await self.event(project_id, 'prompt_imported', {'characters': len(prompt)})
        if final_review:
            await self.event(project_id,'final_review_enabled',{'enabled':True})
        if restarted_from:
            await self.event(project_id, 'workload_restarted', {'source_project_id': restarted_from})
        return await self._launch(project, 'run')

    async def _create_existing(self, name, prompt, target, final_review=False):
        from mobyai.review import minutes
        workspace = self._target(target)
        if not workspace.is_dir() or not any(workspace.iterdir()):
            raise ValueError('Choose a non-empty existing codebase directory')
        native = minutes.native(workspace)
        for id in await self.runtime.store.projects.list_ids():
            other = await self.runtime.store.projects.get(id)
            if other is None: continue
            root = other.workspace_root.resolve()
            if root == workspace:
                if project_is_running(self.runtime.config.persistence.database_path, id):
                    raise ValueError('Another workload is using this codebase')
                native = native or not (workspace/'mobyMinutes'/'manifest.json').exists()
            elif workspace.is_relative_to(root) or root.is_relative_to(workspace):
                raise ValueError('Choose a root that does not overlap another project')
        now = datetime.now(UTC)
        project = Project(project_id='import-'+uuid4().hex[:12],name=name,objective=prompt,
                          workspace_root=workspace,created_at=now,updated_at=now)
        minutes.initialize(project, origin='greenfield' if native else 'external')
        await self.runtime.store.projects.save(project)
        await self.event(project.project_id,'existing_codebase_imported',{'mode':'native' if native else 'external'})
        if final_review:
            await self.event(project.project_id,'final_review_enabled',{'enabled':True})
        return await self._launch(project,'run')

    async def build_final(self, payload):
        from mobyai.final_pass.runtime import build, status
        if not isinstance(payload,dict) or payload.get('action') not in {'build','cancel'}:
            raise ValueError('Choose build or cancel')
        async with self.lock:
            if payload.get('action') == 'cancel':
                if self.final_build_task and not self.final_build_task.done():
                    self.final_build_task.cancel()
                    try: await self.final_build_task
                    except asyncio.CancelledError: pass
                return status(self.runtime.config.final_fail)
            self._assert_idle()
            for id in await self.runtime.store.projects.list_ids():
                if project_is_running(self.runtime.config.persistence.database_path,id):
                    raise ValueError('Stop running workloads before pre-building final inference')
            config=self.runtime.config.final_fail.model_copy(deep=True)
            self.final_build_task=asyncio.create_task(self._build_final_job(config))
            return {'state':'building','message':'Pre-build started; it downloads the configured weights, compiles and smoke-tests Colibri.'}

    async def _build_final_job(self, config):
        from mobyai.final_pass.runtime import build, publish
        try:
            await build(config,self.runtime.config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            publish(config,'failed',str(exc))

    async def edit_plan(self, payload):
        from mobyai.review import editing
        async with self.lock:
            project = await self._project(payload)
            self._assert_stopped(project)
            with project_lease(self.runtime.config.persistence.database_path, project.project_id):
                with workspace_lease(self.runtime.config.persistence.database_path, project.workspace_root):
                    project = await self._project(payload)
                    self._assert_stopped(project)
                    if payload.get('action') == 'load':
                        return await editing.snapshot(self.runtime.store, project)
                    if payload.get('action') != 'save':
                        raise ValueError('Choose load or save')
                    return await editing.save(self.runtime, project, payload)

    async def approve_plan(self, payload):
        from mobyai.review.service import approve
        async with self.lock:
            project=await self._project(payload)
            self._assert_stopped(project)
            with project_lease(self.runtime.config.persistence.database_path, project.project_id):
                with workspace_lease(self.runtime.config.persistence.database_path, project.workspace_root):
                    await approve(self.runtime,project,payload.get('revision'))
            return await self._launch(project,'resume')

    async def report(self, payload):
        async with self.lock:
            project=await self._project(payload)
            if self._busy():
                if self.project_id != project.project_id:
                    raise ValueError('Wait for the other workload before requesting this report')
                await self.event(project.project_id,'logic_report_requested',{})
                return {'status':'queued','message':'Report queued for the next safe step boundary'}
            self._assert_stopped(project)
            await self._launch(project,'report')
            return {'status':'started','message':'Generating reasoning logic report'}

    def log_path(self, project_id):
        name = project_id if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', project_id) else sha256(project_id.encode()).hexdigest()
        return self.runtime.config.persistence.database_path.parent / 'logs' / 'workloads' / (name + '.log')

    async def _launch(self, project, command):
        log = self.log_path(project.project_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log.open('ab') as stream:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, '-u', '-m', 'mobyai', '--config', str(self.config_path),
                    command, project.project_id, stdout=stream, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
        except OSError as exc:
            await self.event(project.project_id, 'workload_launch_failed', {'error': str(exc)})
            raise ValueError(f"Project {project.project_id} saved but launch failed: {exc}") from exc
        self.stop_challenge = None
        self.stop_task = None
        self.project_id = project.project_id
        self.watcher = asyncio.create_task(self.watch(self.process, project.project_id))
        await self.event(project.project_id, 'workload_started', {'log': str(log), 'command': command})
        return {'project_id': project.project_id, 'name': project.name,
                'workspace_root': str(project.workspace_root)}

    async def _project(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('project_id'), str):
            raise ValueError('Select a project')
        project = await self.runtime.store.projects.get(payload['project_id'])
        if project is None:
            raise ValueError('Project not found')
        return project

    def _assert_stopped(self, project):
        self._assert_idle()
        # Unknown owners from older versions cannot be safely interrupted here.
        if project.state in {ProjectState.WORKING, ProjectState.DISPATCHING, ProjectState.VALIDATING,
                             ProjectState.PLANNING, ProjectState.REPLANNING}:
            raise ValueError('Stop this project cleanly before restarting, resuming, or changing its target')
        if project_is_running(self.runtime.config.persistence.database_path, project.project_id):
            raise ValueError('This project is already running')

    async def resume(self, payload):
        async with self.lock:
            project = await self._project(payload)
            self._assert_stopped(project)
            if project.state in {ProjectState.COMPLETED, ProjectState.FAILED}:
                raise ValueError('Completed or failed projects require Restart; Resume preserves existing progress')
            if not project.workspace_root.is_dir():
                raise ValueError('Saved workspace is missing. Restore it before resuming')
            await self.event(project.project_id, 'resume_requested', {})
            return await self._launch(project, 'resume')

    async def approve_timeout(self, payload):
        from mobyai.engine.timeouts import approve_limit
        async with self.lock:
            project = await self._project(payload)
            self._assert_stopped(project)
            return await approve_limit(self.runtime.store,
                self.runtime.config.persistence.database_path, project.project_id,
                payload.get("seconds"), payload.get("confirmation"), source="ui")

    async def delete(self, payload):
        async with self.lock:
            project = await self._project(payload)
            self._assert_stopped(project)
            if payload.get('confirmation') != project.project_id:
                raise ValueError('Type the exact project ID to delete its records')
            with project_lease(self.runtime.config.persistence.database_path, project.project_id):
                await self.runtime.store.delete_project(project.project_id)
            if self.project_id == project.project_id:
                self.project_id = None
                self.process = None
            if self.relocation and self.relocation['project_id'] == project.project_id:
                self.relocation = None
            return {'deleted_project_id':project.project_id,'workspace_preserved':str(project.workspace_root)}

    async def restart(self, payload):
        async with self.lock:
            project = await self._project(payload)
            self._assert_stopped(project)
            if payload.get('confirmation') != project.project_id:
                raise ValueError('Confirm the source project ID to restart')
            with project_lease(self.runtime.config.persistence.database_path, project.project_id):
                return await self._create(project.name, project.objective,
                    payload.get('target_directory'), restarted_from=project.project_id)

    async def change_target(self, payload):
        async with self.lock:
            project = await self._project(payload)
            self._assert_stopped(project)
            source = project.workspace_root.resolve()
            target = self._target(payload.get('target_directory'))
            if target == source:
                return {'project_id': project.project_id, 'workspace_root': str(source)}
            if target.is_relative_to(source) or source.is_relative_to(target):
                raise ValueError('Source and target directories must not contain one another')
            if not source.is_dir():
                raise ValueError('Saved workspace is missing. Restore it before changing its target')
            await self._assert_unused_target(target, project.project_id)
            self._prepare_target(target)
            self.relocation = {'project_id': project.project_id, 'state': 'copying',
                               'target_directory': str(target), 'error': None}
            self.move_task = asyncio.create_task(self._relocate(project, source, target))
            return {'project_id': project.project_id, 'workspace_root': str(source),
                    'target_directory': str(target), 'status': 'copying'}

    async def _relocate(self, project, source, target):
        try:
            with project_lease(self.runtime.config.persistence.database_path, project.project_id):
                await self.event(project.project_id, 'target_copy_started', {'to': str(target)})
                await asyncio.to_thread(shutil.copytree, source, target, symlinks=True, dirs_exist_ok=True)
                for attempt in await self.runtime.store.attempts.list_for_project(project.project_id):
                    log = Path(attempt.test_log_path) if attempt.test_log_path else None
                    if log is not None and log.is_absolute() and log.is_relative_to(source):
                        await self.runtime.store.attempts.save(attempt.model_copy(update={'test_log_path': str(log.relative_to(source))}))
                project = project.model_copy(update={'workspace_root': target, 'updated_at': datetime.now(UTC)})
                await self.runtime.store.projects.save(project)
                await self.event(project.project_id, 'target_changed', {'from': str(source), 'to': str(target)})
                self.relocation['state'] = 'complete'
        except Exception as exc:
            self.relocation['state'] = 'failed'
            self.relocation['error'] = 'Directory change failed. Check the current target before retrying; the original files are retained and the destination may contain a partial copy.'
            await self.event(project.project_id, 'target_change_failed', {'error': str(exc), 'to': str(target)})

    async def event(self, project_id, event, payload):
        await self.runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC),
            project_id=project_id, component='console', event=event, payload=payload))

    async def watch(self, process, project_id):
        code = await process.wait()
        # A failed child may leave WORKING in the database. Reconcile only after
        # exit and while holding both leases; never pause a replacement worker.
        from mobyai.engine.lease import ProjectBusyError
        from mobyai.engine.stopping import pause_interrupted
        try:
            with project_lease(self.runtime.config.persistence.database_path, project_id):
                project = await self.runtime.store.projects.get(project_id)
                if project is not None:
                    with workspace_lease(self.runtime.config.persistence.database_path, project.workspace_root):
                        await pause_interrupted(self.runtime.store, project_id)
        except ProjectBusyError:
            pass  # A new owner is responsible for its own lifecycle.
        await self.event(project_id, 'workload_exited', {'exit_code': code})

    def status(self, project_id, state=None):
        running = self.project_id == project_id and self.process is not None and self.process.returncode is None
        inactive = not self._busy() and state is not None and state not in {
            'WORKING', 'DISPATCHING', 'VALIDATING', 'PLANNING', 'REPLANNING'}
        leased = project_is_running(self.runtime.config.persistence.database_path, project_id)
        inactive = inactive and not leased
        return {"running": bool(running or leased),
                "relocation": self.relocation if self.relocation and self.relocation["project_id"] == project_id else None,
                "can_resume": inactive and state not in {'COMPLETED', 'FAILED'},
                "can_delete": inactive, "can_restart": inactive, "can_change_target": inactive,
                "can_stop": running and not (self.stop_task and not self.stop_task.done()),
                "stopping": bool(running and self.stop_task and not self.stop_task.done())}

    async def prepare_stop(self, payload):
        if not isinstance(payload, dict) or not self.status(payload.get("project_id"))["can_stop"]:
            raise ValueError("This project has no running workload owned by this console")
        token = secrets.token_urlsafe(32)
        self.stop_challenge = (token, self.project_id, self.process.pid, time.monotonic() + 60)
        return {"token": token, "project_id": self.project_id, "expires_in": 60}

    async def stop(self, payload):
        async with self.lock:
            challenge = self.stop_challenge
            if not isinstance(payload, dict) or challenge is None:
                raise ValueError("Request stop confirmation first")
            token, project_id, pid, expires = challenge
            if (payload.get("project_id") != project_id or payload.get("confirmation") != project_id
                or not isinstance(payload.get("token"), str)
                or not secrets.compare_digest(payload["token"], token)
                or time.monotonic() > expires or self.process.pid != pid
                or not self.status(project_id)["can_stop"]):
                raise ValueError("Stop confirmation is incorrect, expired, or no longer applies")
            self.stop_challenge = None
            await self.event(project_id, "stop_requested", {})
            self.stop_task = asyncio.create_task(self._stop_current())
            return {"project_id": project_id, "status": "stopping"}

    async def _stop_current(self):
        from mobyai.engine.stopping import pause_interrupted
        try:
            await self._terminate()
            await pause_interrupted(self.runtime.store, self.project_id)
            await self.event(self.project_id, "stop_completed", {})
        except Exception as exc:
            await self.event(self.project_id, "stop_failed", {"error": str(exc)})
            raise

    async def close(self):
        if self.final_build_task and not self.final_build_task.done():
            self.final_build_task.cancel()
            try: await self.final_build_task
            except asyncio.CancelledError: pass
        if self.move_task is not None:
            await self.move_task
        if self.stop_task is not None:
            await self.stop_task
        elif self.process is not None and self.process.returncode is None:
            await self._stop_current()
        elif self.watcher:
            await self.watcher

    async def _terminate(self):
        if self.process is not None and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 45)
            except TimeoutError:
                await self.event(self.project_id, "stop_forced", {"grace_seconds": 45})
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.process.wait()
        if self.watcher:
            await self.watcher

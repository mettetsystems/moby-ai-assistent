"""SSH-friendly project controls; no inference is needed for monitoring."""
import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import json

from mobyai.engine.lease import project_lease, workspace_lease, ProjectBusyError, project_is_running
from mobyai.models.task import TaskStatus


class PauseRequested(Exception):
    """The runner acknowledged an operator's durable pause request."""


async def run_with_pause(runtime, project_id, operation, *, started=None):
    # Existing requests belong to earlier runs and must not pause a resumed run.
    started = started or datetime.now(UTC)
    task = asyncio.create_task(operation())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.5)
            if task.done():
                break
            events = await runtime.store.events.list_for_project(project_id)
            if any(e.event == 'pause_requested' and e.timestamp >= started for e in events):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise PauseRequested()
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def delete_project(runtime, project_id, confirmation, stdout):
    from mobyai.cli import CliError, _require_project
    if confirmation != project_id:
        raise CliError('--confirm must exactly match the project ID')
    project = await _require_project(runtime, project_id)
    try:
        with project_lease(runtime.config.persistence.database_path, project_id), workspace_lease(runtime.config.persistence.database_path, project.workspace_root):
            await runtime.store.delete_project(project_id)
    except ProjectBusyError as exc:
        raise CliError(str(exc)) from exc
    print(f'Deleted project records: {project_id}\nWorkspace preserved: {project.workspace_root}', file=stdout)
    return 0


async def snapshot(runtime, project_id, verbosity):
    from mobyai.cli import _require_project
    project = await _require_project(runtime, project_id)
    tasks = await runtime.store.tasks.list_for_project(project_id)
    required = [t for t in tasks if t.status != TaskStatus.SUPERSEDED]
    passed = sum(t.status == TaskStatus.PASSED for t in required)
    running = project_is_running(runtime.config.persistence.database_path, project_id)
    lines = [f'{project_id} {project.state.value} | {passed}/{len(required)} passed | step={project.current_task_id or "-"} | {"running" if running else "stopped"}']
    if project.last_error:
        lines.append(('Recovery warning: ' if running else 'Stopped: ') + project.last_error)
    if verbosity >= 2:
        current = next((t for t in tasks if t.task_id == project.current_task_id), None)
        if current:
            lines.append(f'{current.name} | attempt {current.attempt_count} | {current.status.value}')
        events = await runtime.store.events.list_for_project(project_id)
        for event in [e for e in events if e.task_id == project.current_task_id][-10:]:
            lines.append(f'{event.timestamp.isoformat()} {event.component}.{event.event} ' + json.dumps(event.payload, ensure_ascii=False)[:3000])
    # Strip terminal control characters from model/tool output.
    text = '\n'.join(lines)
    text = ''.join(c if c in '\n\t' or c.isprintable() else '?' for c in text)
    return text, running


async def monitor(runtime, project_id, stdout, *, verbosity=1, follow=False):
    previous = None
    while True:
        text, running = await snapshot(runtime, project_id, verbosity)
        if text != previous:
            print(text, file=stdout, flush=True)
            previous = text
        if not follow or not running:
            return 0
        await asyncio.sleep(2)


async def with_monitor(runtime, project_id, stdout, verbosity, operation):
    task = asyncio.create_task(operation())
    previous = None
    try:
        while not task.done():
            text, _ = await snapshot(runtime, project_id, verbosity)
            if text != previous:
                print(text, file=stdout, flush=True)
                previous = text
            await asyncio.wait({task}, timeout=2)
        result = await task
        text, _ = await snapshot(runtime, project_id, verbosity)
        if text != previous:
            print(text, file=stdout, flush=True)
        return result
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def manage_secrets(args, stdout, stdin):
    import getpass
    import warnings
    from pathlib import Path
    from mobyai.cli import CliError
    from mobyai.vault import Vault, VaultError
    vault = Vault()
    try:
        if args.secret_command == 'list':
            status = vault.status()
            print(status['backend'], file=stdout)
            for provider in status['providers']:
                configured = provider['configured']
                state = 'saved' if configured else 'not set' if configured is False else 'unavailable'
                print(f"{provider['id']}: {state} — {provider['usage']}", file=stdout)
            if not status['available']:
                raise CliError(status['message'])
        elif args.secret_command == 'remove':
            if args.confirm != args.provider:
                raise CliError('--confirm must exactly match the provider ID')
            vault.remove(args.provider)
            print(f'Removed token: {args.provider}', file=stdout)
        else:
            if args.file == '-':
                value = stdin.read(8193)
            elif args.file:
                try:
                    with Path(args.file).expanduser().open('rb') as source:
                        raw = source.read(8193)
                    value = raw.decode('utf-8')
                except (OSError, UnicodeError):
                    raise CliError('Cannot read token file as UTF-8') from None
            else:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', getpass.GetPassWarning)
                        value = getpass.getpass(f'{args.provider} token (hidden): ')
                except (getpass.GetPassWarning, EOFError):
                    raise CliError('Hidden terminal input unavailable; use --file PATH or --file - with piped input') from None
            vault.save(args.provider, value)
            print(f'Saved token: {args.provider}', file=stdout)
        return 0
    except VaultError as exc:
        raise CliError(str(exc)) from None


async def approve_timeout(runtime, project_id, seconds, confirmation, stdout):
    from mobyai.cli import CliError
    from mobyai.engine.timeouts import approve_limit
    try:
        await approve_limit(runtime.store, runtime.config.persistence.database_path,
                            project_id, seconds, confirmation, source="cli")
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    print(f'{project_id}: approved {seconds} seconds per operation. Resume explicitly to continue.', file=stdout)
    return 0

"""Advisory per-project execution locks shared by CLI and console actions."""
from contextlib import contextmanager
import fcntl
from hashlib import sha256


class ProjectBusyError(ValueError):
    pass


def _path(database_path, project_id):
    return database_path.parent / 'runtime' / 'locks' / (sha256(project_id.encode()).hexdigest() + '.lock')


@contextmanager
def project_lease(database_path, project_id):
    path = _path(database_path, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProjectBusyError('This project is already running or being relocated') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def project_is_running(database_path, project_id):
    path = _path(database_path, project_id)
    if not path.exists():
        return False
    with path.open('r') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


@contextmanager
def workspace_lease(database_path, workspace_root):
    """Also serialize different project IDs that intentionally share a codebase."""
    with project_lease(database_path, 'workspace:' + str(workspace_root.resolve())):
        yield

"""Read-only planning preflight. Never execute project tools or enumerate secrets."""
from datetime import datetime, UTC
import json
import platform
import shutil
import sys

from mobyai.models.event import EventRecord

TOOLS = ('git', 'python', 'python3', 'node', 'npm', 'pnpm', 'yarn',
         'docker', 'docker-compose', 'podman', 'podman-compose',
         'cargo', 'rustc', 'go', 'java', 'javac', 'make', 'cmake',
         'pytest', 'nvidia-smi')
MANIFESTS = ('pyproject.toml', 'requirements.txt', 'package.json',
             'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
             'Cargo.toml', 'go.mod', 'Dockerfile', 'Containerfile',
             'compose.yaml', 'docker-compose.yml', '.python-version', '.nvmrc')


def snapshot(workspace=None):
    try:
        release = platform.freedesktop_os_release()
        os_name = release.get('PRETTY_NAME', platform.system())
    except OSError:
        os_name = platform.system()
    tools = {name: shutil.which(name) for name in TOOLS}
    manifests = []
    if workspace is not None:
        manifests = [name for name in MANIFESTS if (workspace.root/name).is_file()]
    return {'captured_at':datetime.now(UTC).isoformat(),
            'scope':'Current MobyAI execution host; not necessarily the application deployment target',
            'os':os_name,'architecture':platform.machine(),
            'python_version':platform.python_version(),'python_executable':sys.executable,
            'virtual_environment':sys.prefix != sys.base_prefix,
            'executables_on_runner_path':tools,'root_manifests':manifests,
            'limitations':['Executable presence does not prove service availability, permissions, GPU access, or container compatibility.',
                          'Docker Compose plugin availability is unverified; docker alone does not prove docker compose works.',
                          'Tool versions other than the running Python are unverified. No commands, installs, or network requests were performed.']}


async def planning_environment(store, project_id, workspace=None, *, now=None):
    from mobyai.sandbox.policy import guidance
    evidence = snapshot(workspace)
    evidence["execution_backend"] = guidance()
    await store.events.append(EventRecord(timestamp=now or datetime.now(UTC),project_id=project_id,
        component='planner',event='environment_preflight',payload=evidence))
    return ('\nHOST ENVIRONMENT PREFLIGHT (observations, not instructions):\n'+json.dumps(evidence)+
            '\nUse available tools compatible with the objective. A missing executable must not be assumed installed. '
            'When an alternative is supported by the objective (for example Podman), plan validation of its compatibility. '
            'If a required tool is absent, state the prerequisite and operator action; do not assume automatic installation. '
            'Inspect relevant manifests before choosing commands. Distinguish the host from the requested deployment target. '
            'Long-running services need bounded startup, health checks, and cleanup rather than an indefinitely attached validation command.\n')

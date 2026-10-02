"""Rootless Podman boundary with fixed mounts and no runtime networking."""
import asyncio
import os
from pathlib import Path
import tempfile
import time
import uuid
from mobyai.execution.commands import CommandResult, _BoundedBuffer, _drain
from .downloads import prepare

PODMAN = "/usr/bin/podman"
PROTECTED = (".git", "mobyMinutes", "MobyMinutes", ".agent")

async def control(*args):
    process = await asyncio.create_subprocess_exec(PODMAN, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-2000:])
    return stdout.decode().strip()

def container_args(config, workspace, cwd, dependencies, name, command):
    if workspace.root in (Path.home(), Path("/")):
        raise ValueError("Sandbox requires a dedicated project directory")
    for path in (workspace.root, dependencies):
        if any(c in str(path) for c in (",", ":", "\n")):
            raise ValueError("Unsupported sandbox mount path")
    relative = workspace.resolve(cwd).relative_to(workspace.root)
    args = [PODMAN, "run", "--name", name, "--pull=never", "--rm",
        "--network=none", "--http-proxy=false", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--userns=keep-id", "--pids-limit", str(config.pids),
        "--memory", f"{config.memory_mb}m", "--cpus", str(config.cpus),
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--workdir", "/workspace/"+relative.as_posix(),
        "--env", "HOME=/tmp", "--env", "PIP_NO_INDEX=1",
        "--env", "PIP_FIND_LINKS=/dependencies", "--env", "npm_config_offline=true",
        "--volume", f"{workspace.root}:/workspace:rw,Z",
        "--volume", f"{dependencies}:/dependencies:ro,Z"]
    # A second mount of the SAME project supports absolute links and generated
    # venv shebangs containing its original host path. It exposes no parent/home.
    aliases = ["/workspace"]
    native = str(workspace.root)
    if native != "/workspace":
        if native in ("/dependencies", "/tmp", "/usr", "/etc", "/opt", "/home", "/var", "/run", "/proc", "/sys", "/dev"):
            raise ValueError("Sandbox requires a dedicated project subdirectory")
        args += ["--volume", f"{workspace.root}:{native}:rw,Z"]
        aliases.append(native)
    for entry in PROTECTED:
        path = workspace.root / entry
        if path.is_symlink():
            raise ValueError(f"Protected path cannot be a symlink: {entry}")
        if path.exists():
            for alias in aliases:
                args += ["--volume", f"{path}:{alias}/{entry}:ro,Z"]
    return args + ["--entrypoint", command[0], config.image, *command[1:]]

async def run(config, workspace, command, cwd, timeout_seconds, max_output_bytes, *, name=None):
    if not command or any(not isinstance(s,str) or not s for s in command):
        raise ValueError("Command must contain nonempty argv strings")
    if os.geteuid() == 0:
        raise ValueError("Sandbox requires rootless Podman")
    controller = Path(__file__).resolve().parents[3]
    if controller.is_relative_to(workspace.root) or workspace.root.is_relative_to(controller):
        raise ValueError("Sandbox workspace must be separate from the MobyAI installation")
    validate_tree(workspace.root)
    name = name or "mobyai-sandbox-" + uuid.uuid4().hex
    import re
    if not re.fullmatch(r"mobyai-sandbox-[a-f0-9]{32}", name):
        raise ValueError("Invalid sandbox lifecycle ID")
    started = time.monotonic()
    stdout = _BoundedBuffer(max_output_bytes)
    stderr = _BoundedBuffer(max_output_bytes)
    process = None
    timed_out = False
    cache = Path.home() / ".cache" / "mobyai-sandbox"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache.resolve().is_relative_to(workspace.root):
        raise ValueError("Sandbox cannot mount its controller cache")
    with tempfile.TemporaryDirectory(prefix="dependencies-", dir=cache) as directory:
        try:
            async with asyncio.timeout(timeout_seconds):
                if await control("info", "--format", "{{.Host.Security.Rootless}}") != "true":
                    raise ValueError("Sandbox requires rootless Podman")
                dependencies = Path(directory)
                await prepare(config.artifacts, dependencies)
                args = container_args(config, workspace, cwd, dependencies, name, command)
                process = await asyncio.create_subprocess_exec(*args,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await _drain(process, stdout, stderr)
        except TimeoutError:
            timed_out = True
        finally:
            cleanup = asyncio.create_task(control("rm", "--force", "--time", "0", "--ignore", name))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
        return CommandResult(command=command, exit_code=-1 if timed_out else process.returncode,
            stdout=stdout.text(), stderr=stderr.text(), duration_seconds=time.monotonic()-started,
            timed_out=timed_out, stdout_truncated=stdout.truncated, stderr_truncated=stderr.truncated)


def validate_tree(root):
    """Inspect directory entries without following links into the host filesystem.

    Symlink destinations resolve in the container namespace, not on the host.
    Hard links and mounted special files still expose host objects and are denied.
    """
    import stat
    def failed(error):
        raise ValueError(f"Cannot inspect sandbox workspace: {error}") from error
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        for entry in dirs + files:
            path = Path(parent) / entry
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if (not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode)) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                raise ValueError(f"Sandbox disallows hard links and special files: {path.relative_to(root)}")

"""Harness-owned image preparation: fixed recipe, no project code in build context."""
import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import fcntl

from .backend import PODMAN, control, run
import signal
from .policy import SandboxConfig
from mobyai.execution.commands import _BoundedBuffer, _drain, _kill_process_group
from mobyai.execution.workspace import Workspace

BASE = "registry.fedoraproject.org/fedora@sha256:a7a70ddf75df79b305c399a1850d086c3aadee16ae614595a0d88ac0e5886a67"
REQUIREMENTS = """huggingface_hub>=0.20,<1
PyQt5>=5.15,<6
requests>=2.31,<3
PyYAML>=6,<7
pytest>=7.4,<10
keyring>=25,<26
SecretStorage>=3.3,<4
pytest-mock>=3,<4
pytest-qt>=4,<5
"""
RECIPE = "FROM " + BASE + """
RUN dnf -y install python3 python3-pip git nodejs npm mesa-libGL libxkbcommon-x11 dbus-libs glib2 fontconfig freetype dejavu-sans-fonts && dnf clean all
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /opt/sandbox/requirements.txt
RUN python -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --no-cache-dir -r /opt/sandbox/requirements.txt && python -m pip check && python -m pip freeze > /opt/sandbox/requirements.lock
RUN rpm -qa | sort > /opt/sandbox/rpm-packages.txt
WORKDIR /workspace
CMD ["python", "--version"]
"""

class SandboxPreparationError(Exception):
    pass

def recipe_key():
    return sha256((RECIPE + "\n" + REQUIREMENTS).encode()).hexdigest()

@asynccontextmanager
async def build_lock(path):
    with path.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.2)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

async def installed(image):
    try:
        identity = await control("image", "inspect", image, "--format", "{{.Id}}")
    except RuntimeError:
        return None
    identity = identity if identity.startswith("sha256:") else "sha256:" + identity
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
        raise SandboxPreparationError("Invalid image identity from Podman")
    return identity

async def ensure(config, seconds, lifecycle_name):
    if not config.enabled:
        raise SandboxPreparationError("Sandbox is not enabled")
    if os.geteuid() == 0 or await control("info", "--format", "{{.Host.Security.Rootless}}") != "true":
        raise SandboxPreparationError("Automatic preparation requires rootless Podman")
    if config.image:
        image = await installed(config.image)
        if image is None:
            raise SandboxPreparationError("Pinned sandbox image is absent; set auto_prepare with an empty image to build the trusted profile")
        return {"image":image,"cached":True,"profile":"operator-pinned"}
    if not config.auto_prepare:
        raise SandboxPreparationError("Automatic image preparation is not enabled")
    folder = Path.home()/".cache"/"mobyai-sandbox"/"images"/recipe_key()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with asyncio.timeout(seconds):
        async with build_lock(folder/"build.lock"):
            record = folder/"image.json"
            if record.exists():
                try:
                    cached = json.loads(record.read_text())
                    image = await installed(cached["image"])
                    if image == cached["image"] and cached.get("recipe_key") == recipe_key():
                        return {**cached,"cached":True}
                except (ValueError, KeyError, TypeError):
                    pass
            (folder/"Containerfile").write_text(RECIPE)
            (folder/"requirements.txt").write_text(REQUIREMENTS)
            # Context contains only the trusted recipe and requirements, never a project.
            context = folder/"context"
            context.mkdir(exist_ok=True)
            (context/"Containerfile").write_text(RECIPE)
            (context/"requirements.txt").write_text(REQUIREMENTS)
            tag = "localhost/mobyai-auto:" + recipe_key()[:24]
            output, errors = _BoundedBuffer(1000000), _BoundedBuffer(1000000)
            process = await asyncio.create_subprocess_exec(PODMAN,"build","--pull=missing",
                "--http-proxy=false","--force-rm","--tag",tag,"--file",str(context/"Containerfile"),str(context),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
            active = active_record(lifecycle_name)
            active.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            active.write_text(json.dumps({"pid":process.pid,"start":process_start(process.pid)}))
            try:
                await _drain(process,output,errors)
            finally:
                if process.returncode is None:
                    _kill_process_group(process.pid)
                    await process.wait()
                active.unlink(missing_ok=True)
                (folder/"build.log").write_text(output.text()+"\n"+errors.text())
            if process.returncode:
                raise SandboxPreparationError("Sandbox image build failed; see " + str(folder/"build.log") + "\n" + errors.text()[-2000:])
            image = await installed(tag)
            if image is None:
                raise SandboxPreparationError("Build did not produce a usable image")
            # Smoke-check the real restricted backend before publishing a cache entry.
            probe = folder/"probe"
            probe.mkdir(exist_ok=True)
            verified = await run(config.model_copy(update={"image":image,"artifacts":[]}),Workspace(probe),
                ["sh","-c", "python -m pip check && node --version && npm --version && python -c \"from PyQt5.QtWidgets import QApplication; app=QApplication([]); print('Qt offscreen OK')\""],
                probe,60,10000,name=lifecycle_name)
            (folder/"verification.json").write_text(verified.model_dump_json(indent=2))
            if verified.exit_code != 0:
                raise SandboxPreparationError("Sandbox image verification failed; see " + str(folder/"verification.json"))
            result={"image":image,"cached":False,"profile":config.profile,"recipe_key":recipe_key(),"build_log":str(folder/"build.log")}
            temporary=folder/"image.json.tmp"
            temporary.write_text(json.dumps(result,indent=2))
            temporary.replace(record)
            return result


def active_record(name):
    if not re.fullmatch(r"mobyai-sandbox-[a-f0-9]{32}", name):
        raise ValueError("Invalid build lifecycle identity")
    return Path.home()/".cache"/"mobyai-sandbox"/"active"/(name+".json")

def process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")",1)[1].split()[19]
    except FileNotFoundError:
        return None

def cleanup_build(name):
    record=active_record(name)
    if not record.exists():
        return
    state=json.loads(record.read_text())
    pid=state["pid"]
    # Never kill a recycled PID or an unrelated process group.
    if type(pid) is int and pid>1 and state["start"] is not None and process_start(pid)==state["start"]:
        try:
            if os.getpgid(pid)==pid:
                _kill_process_group(pid)
        except ProcessLookupError:
            pass
    record.unlink(missing_ok=True)

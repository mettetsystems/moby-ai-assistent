import asyncio
from hashlib import sha256
import json
from pathlib import Path
import httpx
import pytest
from pydantic import ValidationError
from mobyai.sandbox.policy import SandboxConfig, Artifact, activate
from mobyai.sandbox.downloads import validate_url, prepare
from mobyai.sandbox.backend import container_args
from mobyai.execution.commands import CommandRunner, CommandResult
from mobyai.execution.workspace import Workspace
from mobyai.execution.tests import TestRunner
from mobyai.execution.diagnostics import diagnose

IMAGE = "sha256:" + "a" * 64

def test_policy_requires_pinned_image_and_fixed_network():
    assert not SandboxConfig().enabled
    for args in ({"enabled":True,"image":"fedora:latest"}, {"network":"host"}, {"privileged":True}):
        with pytest.raises(ValidationError):
            SandboxConfig(**args)

@pytest.mark.parametrize("url", [
    "http://files.pythonhosted.org/a.whl", "https://localhost/a.whl",
    "https://registry.npmjs.org.evil.test/a.tgz", "https://user@registry.npmjs.org/a.tgz",
    "https://registry.npmjs.org:8080/a.tgz", "https://registry.npmjs.org/a.tgz?secret=x",
    "https://registry.npmjs.org/install.sh",
])
def test_download_destinations_restricted(url):
    with pytest.raises(ValueError): validate_url(url)

async def test_download_hash_and_redirects(tmp_path):
    artifact = Artifact(filename="a.whl",url="https://files.pythonhosted.org/a.whl",sha256=sha256(b"wheel").hexdigest())
    await prepare([artifact],tmp_path,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b"wheel")))
    assert (tmp_path/'a.whl').read_bytes()==b"wheel"
    (tmp_path/'a.whl').unlink()
    for response in (httpx.Response(302,headers={"Location":"http://localhost/"}),httpx.Response(200,content=b"wrong")):
        with pytest.raises(ValueError):
            await prepare([artifact],tmp_path,transport=httpx.MockTransport(lambda r:response))
        assert not (tmp_path/'a.whl').exists()

def test_container_boundary(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'mobyMinutes').mkdir()
    dependencies=tmp_path/'deps';dependencies.mkdir()
    args=container_args(SandboxConfig(enabled=True,image=IMAGE),Workspace(root),root,dependencies,'mobyai-test',['sh','-c','echo ok'])
    for flag in ('--network=none','--read-only','--pull=never','--cap-drop=ALL','--security-opt=no-new-privileges','--userns=keep-id'):
        assert flag in args
    assert f'{root}/mobyMinutes:/workspace/mobyMinutes:ro,Z' in args
    assert '--privileged' not in args
    assert args[-5:]==['--entrypoint','sh',IMAGE,'-c','echo ok']
    with pytest.raises(Exception):
        container_args(SandboxConfig(enabled=True,image=IMAGE),Workspace(root),tmp_path,dependencies,'test',['true'])

async def test_commands_and_harness_share_backend_without_host_fallback(tmp_path,monkeypatch):
    async def remote(*args):
        return CommandResult(command=args[2],exit_code=125,stdout='',stderr='Sandbox unavailable: test',duration_seconds=0)
    async def forbidden(*args,**kwargs):
        raise AssertionError('host execution attempted')
    monkeypatch.setattr('mobyai.sandbox.client.execute',remote)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',forbidden)
    workspace=Workspace(tmp_path);runner=CommandRunner(workspace)
    with activate(SandboxConfig(enabled=True,image=IMAGE)):
        result=await TestRunner(runner,workspace).run(['sudo','true'])
    assert result.exit_code==125
    assert diagnose(result.stderr).category=='execution_policy'

async def test_host_sudo_validation_is_reported_not_uncaught(tmp_path):
    workspace=Workspace(tmp_path)
    result=await TestRunner(CommandRunner(workspace),workspace).run(['sudo','true'])
    assert result.exit_code==126
    assert diagnose(result.stderr).category=='execution_policy'
    assert not diagnose(result.stderr).sampling_eligible

async def test_cleanup_name_is_harness_owned(tmp_path,monkeypatch):
    from mobyai.sandbox import client
    from contextlib import asynccontextmanager
    seen=[]
    @asynccontextmanager
    async def broken(params):
        seen.append(params.env["MOBYAI_SANDBOX_NAME"])
        raise RuntimeError("MCP transport failed")
        yield
    async def cleanup(*args):
        assert args[:5]==("rm","--force","--time","0","--ignore")
        assert args[-1]==seen[0]
    monkeypatch.setattr('mcp.client.stdio.stdio_client',broken)
    monkeypatch.setattr('mobyai.sandbox.backend.control',cleanup)
    result=await client.execute(SandboxConfig(enabled=True,image=IMAGE),Workspace(tmp_path),['true'],tmp_path,30,1024)
    assert result.exit_code==125
    assert seen[0].startswith('mobyai-sandbox-')

async def test_cleanup_error_is_visible_not_uncaught(tmp_path,monkeypatch):
    from mobyai.sandbox import client
    async def failed(*args):
        raise RuntimeError("container cleanup failed")
    monkeypatch.setattr(client,'_execute',failed)
    result=await client.execute(SandboxConfig(enabled=True,image=IMAGE),Workspace(tmp_path),['true'],tmp_path,30,1024)
    assert result.exit_code==125
    assert 'container cleanup failed' in result.stderr

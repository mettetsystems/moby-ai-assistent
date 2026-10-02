"""Opt-in integration: uses only a supplied, already-local image, never pulls."""
import asyncio
import os
import subprocess
import pytest
from mobyai.sandbox.policy import SandboxConfig
from mobyai.sandbox.client import execute
from mobyai.execution.workspace import Workspace

pytestmark=pytest.mark.skipif(not os.environ.get("MOBYAI_SANDBOX_TEST_IMAGE"),reason="Needs a local container image and rootless Podman")

async def test_mcp_boundary_timeout_and_cancel(tmp_path):
    config=SandboxConfig(enabled=True,image=os.environ["MOBYAI_SANDBOX_TEST_IMAGE"])
    root=tmp_path/'project';root.mkdir();(root/'mobyMinutes').mkdir()
    workspace=Workspace(root)
    before=set(subprocess.check_output(['/usr/bin/podman','ps','-a','--filter','name=mobyai-sandbox-','--format','{{.Names}}']).decode().splitlines())
    result=await execute(config,workspace,['sh','-c',
        'echo ok > result.txt; test ! -e /var/run/docker.sock && ! touch /forbidden && ! touch mobyMinutes/forbidden && test "$(ls /sys/class/net)" = lo'],root,30,4096)
    assert result.exit_code==0,result.stderr
    assert (root/'result.txt').read_text().strip()=='ok'
    result=await execute(config,workspace,['sleep','30'],root,2,4096)
    assert result.timed_out
    task=asyncio.create_task(execute(config,workspace,['sleep','60'],root,90,4096))
    try:
        for _ in range(40):
            await asyncio.sleep(.2)
            running=set(subprocess.check_output(['/usr/bin/podman','ps','--filter','name=mobyai-sandbox-','--format','{{.Names}}']).decode().splitlines())
            if running-before:break
        assert running-before,'Container did not start'
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    after=set(subprocess.check_output(['/usr/bin/podman','ps','-a','--filter','name=mobyai-sandbox-','--format','{{.Names}}']).decode().splitlines())
    assert after==before

async def test_mcp_symlinks_preserve_isolation(tmp_path):
    import json
    root=tmp_path/'project';root.mkdir();(root/'mobyMinutes').mkdir()
    (root/'data').write_text('original')
    (root/'relative').symlink_to('data')
    (root/'chain').symlink_to('relative')
    (root/'absolute').symlink_to(root/'data')
    (root/'loop').symlink_to('loop')
    (root/'dangling').symlink_to('absent')
    (root/'python').symlink_to('/usr/bin/python3')
    outside=tmp_path/'host-only';outside.write_text('private')
    (root/'external').symlink_to(outside)
    code="""from pathlib import Path
import os
assert Path('chain').read_text()=='original'
assert Path('absolute').read_text()=='original'
Path('absolute').write_text('updated')
assert Path('relative').read_text()=='updated'
assert not Path('external').exists()
assert os.readlink('loop')=='loop'
assert not Path('dangling').exists()
for base in ['/workspace/mobyMinutes', NATIVE_HISTORY]:
    try: Path(base,'blocked').write_text('bad')
    except OSError: pass
    else: raise AssertionError('history is writable')
""".replace('NATIVE_HISTORY',repr(str(root/'mobyMinutes')))
    result=await execute(SandboxConfig(enabled=True,image=os.environ['MOBYAI_SANDBOX_TEST_IMAGE']),Workspace(root),['./python','-c',code],root,30,4096)
    assert result.exit_code==0,result.stderr
    assert (root/'data').read_text()=='updated'
    assert outside.read_text()=='private'

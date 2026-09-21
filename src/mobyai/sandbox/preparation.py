"""Prepare an image before planning; cancellation remains under workload control."""
import asyncio
from datetime import UTC, datetime
import json
import os
import sys
import time
import uuid
from .builder import SandboxPreparationError, cleanup_build, recipe_key
from .backend import control
from .policy import activate
from mobyai.engine.timeouts import current_policy, operation_key
from mobyai.models.event import EventRecord

async def _call_prepare(config,seconds):
    name="mobyai-sandbox-"+uuid.uuid4().hex
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env={k:os.environ[k] for k in ("HOME","PATH","XDG_RUNTIME_DIR","DBUS_SESSION_BUS_ADDRESS") if k in os.environ}
        env.update(MOBYAI_SANDBOX_NAME=name,MOBYAI_SANDBOX_POLICY=config.model_dump_json(),MOBYAI_SANDBOX_TIMEOUT=str(seconds))
        params=StdioServerParameters(command=sys.executable,args=["-m","mobyai.sandbox.server"],env=env)
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                result=await session.call_tool("sandbox_prepare",{})
                if result.isError:
                    raise SandboxPreparationError(" ".join(c.text for c in result.content if c.type=="text")[:2500])
                return json.loads(next(c.text for c in result.content if c.type=="text"))
    except Exception as exc:
        raise SandboxPreparationError(f"Sandbox preparation failed: {exc}") from exc
    finally:
        cleanup_build(name)
        cleanup=asyncio.create_task(control("rm","--force","--time","0","--ignore",name))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

async def call_prepare(config,seconds):
    try:
        return await _call_prepare(config,seconds)
    except SandboxPreparationError:
        raise
    except Exception as exc:
        raise SandboxPreparationError(f"Sandbox preparation/cleanup failed: {exc}") from exc

async def run_prepared(runtime,project,stdout,operation):
    config=runtime.config.sandbox
    if not config.enabled:
        return await operation()
    async def record(event,payload):
        await runtime.store.events.append(EventRecord(timestamp=datetime.now(UTC),project_id=project.project_id,
            component="sandbox",event=event,payload=payload))
    policy=current_policy()
    key=operation_key("sandbox_prepare",[config.profile,config.image,recipe_key()])
    seconds=policy.budget(key,600) if policy else 600
    print("sandbox prepare: checking/building trusted image",file=stdout,flush=True)
    await record("sandbox_preparing",{"profile":config.profile,"deadline_seconds":seconds})
    started=time.monotonic()
    pending=asyncio.create_task(call_prepare(config,seconds))
    try:
        while not pending.done():
            done,_=await asyncio.wait({pending},timeout=10)
            if not done:
                elapsed=round(time.monotonic()-started)
                print(f"sandbox prepare: {elapsed}s elapsed",file=stdout,flush=True)
                await record("sandbox_preparing",{"profile":config.profile,"elapsed_seconds":elapsed})
        result=await pending
        if result.get("timed_out"):
            next_seconds=await policy.timed_out(key,seconds,"sandbox image preparation") if policy else seconds
            raise SandboxPreparationError(f"Image preparation timed out after {seconds}s. Resume permits {next_seconds}s; no workload commands ran.")
        # Validate the MCP response before installing the effective execution policy.
        effective=config.model_copy(update={"auto_prepare":False,"image":result["image"]})
        effective=type(config).model_validate(effective.model_dump())
        await record("sandbox_ready",result)
        print(f"sandbox ready: {effective.image} ({'cached' if result.get('cached') else 'built and verified'})",file=stdout,flush=True)
        with activate(effective):
            return await operation()
    except SandboxPreparationError as exc:
        await record("sandbox_preparation_failed",{"error":str(exc)})
        raise
    finally:
        if not pending.done():
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass

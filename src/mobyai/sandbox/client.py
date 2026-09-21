"""Failed isolation never falls back to a host command."""
import asyncio
import uuid
import os
import sys
import time
from mobyai.execution.commands import CommandResult

async def _execute(config, workspace, command, cwd, seconds, output_limit):
    started = time.monotonic()
    name = "mobyai-sandbox-" + uuid.uuid4().hex
    session_started = False
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {k: os.environ[k] for k in ("HOME", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ}
        env.update(MOBYAI_SANDBOX_NAME=name, MOBYAI_SANDBOX_POLICY=config.model_dump_json(),
                   MOBYAI_SANDBOX_WORKSPACE=str(workspace.root),
                   MOBYAI_SANDBOX_TIMEOUT=str(seconds), MOBYAI_SANDBOX_OUTPUT_LIMIT=str(output_limit))
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "mobyai.sandbox.server"], env=env)
        session_started = True
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("sandbox_execute", {
                    "command":command, "cwd":workspace.relative(cwd), "timeout_seconds":seconds})
                if result.isError:
                    raise RuntimeError(" ".join(c.text for c in result.content if c.type == "text")[:2000])
                return CommandResult.model_validate_json(next(c.text for c in result.content if c.type == "text"))
    except Exception as exc:
        return CommandResult(command=command, exit_code=125, stdout="",
            stderr=f"Sandbox unavailable: {exc}. Host execution was not attempted.",
            duration_seconds=time.monotonic()-started)
    finally:
        if session_started:
            # MCP disconnect can terminate the child before its finally block runs.
            # Parent-owned identity permits cleanup without trusting worker inputs.
            from .backend import control
            cleanup = asyncio.create_task(control("rm", "--force", "--time", "0", "--ignore", name))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise


async def execute(config, workspace, command, cwd, seconds, output_limit):
    started = time.monotonic()
    try:
        return await _execute(config, workspace, command, cwd, seconds, output_limit)
    except Exception as exc:
        # Cleanup/startup errors also become visible environment blockers.
        # CancelledError is intentionally not caught: pauses still propagate.
        return CommandResult(command=command, exit_code=125, stdout="",
            stderr=f"Sandbox unavailable: {exc}. Host execution was not attempted.",
            duration_seconds=time.monotonic()-started)

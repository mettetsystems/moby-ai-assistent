"""Harness-owned MCP processes. No arbitrary server, shell or endpoint inputs."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import sys
from uuid import uuid4

from mobyai.execution.tools import ToolOutcome
from mobyai.local_tools.contracts import TOOLS
from mobyai.records.capture import emit


@asynccontextmanager
async def session_for(group, workspace, *, read_only=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if group not in {"workspace", "environment"}:
        raise ValueError("Unknown local MCP server")
    env = {key: os.environ[key] for key in ("HOME", "PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ}
    env.update(MOBYAI_LOCAL_WORKSPACE=str(workspace.root),
               MOBYAI_LOCAL_EXTRA_PATHS=json.dumps([str(path) for path in workspace.extra_allowed]),
               MOBYAI_LOCAL_READ_ONLY="1" if read_only else "0")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "mobyai.local_tools.server", group], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_local_tool(name, arguments, workspace, *, timeout_seconds=60):
    definition = TOOLS[name]
    # Reject malformed calls before launching a child, as well as on the server.
    validated = definition.arguments.model_validate(arguments).model_dump()
    identity = uuid4()
    await emit("local_mcp_started", {"server": definition.group, "tool": name}, correlation_id=identity)
    try:
        async with asyncio.timeout(min(max(timeout_seconds, 1), 60)):
            async with session_for(definition.group, workspace, read_only=definition.read_only) as session:
                result = await session.call_tool(name, validated)
                payload = result.structuredContent
                if payload is None:
                    text = "\n".join(part.text for part in result.content if part.type == "text")
                    if result.isError:
                        raise ValueError(text[:2000])
                    payload = json.loads(text)
                outcome = ToolOutcome.model_validate(payload)
                if outcome.name.value != name or bool(result.isError) != (not outcome.ok):
                    raise ValueError("MCP returned an inconsistent tool result")
    except asyncio.CancelledError:
        await emit("local_mcp_cancelled", {"server": definition.group, "tool": name}, correlation_id=identity)
        raise
    except ImportError:
        outcome = ToolOutcome(name=name, ok=False, error="Local MCP SDK unavailable; install the project's declared dependencies.")
    except Exception as exc:
        outcome = ToolOutcome(name=name, ok=False,
            error=f"Local MCP {definition.group} failed: {type(exc).__name__}: {str(exc)[:2000]}. "
                  "Check workspace state before retrying; the call was not replayed.")
    await emit("local_mcp_finished", {"server": definition.group, "tool": name,
               "ok": outcome.ok, "error": outcome.error}, correlation_id=identity)
    return outcome

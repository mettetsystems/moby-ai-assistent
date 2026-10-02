"""Two local stdio MCP servers: `python -m mobyai.local_tools.server GROUP`."""
import argparse
import asyncio
import json
import os

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from mobyai.execution.tools import ToolOutcome
from mobyai.execution.workspace import Workspace
from mobyai.local_tools.contracts import TOOLS
from mobyai.local_tools.operations import OPERATIONS


def create_server(group: str, workspace: Workspace, *, read_only: bool = False) -> Server:
    definitions = {name: definition for name, definition in TOOLS.items()
                   if definition.group == group and (not read_only or definition.read_only)}
    if not definitions:
        raise ValueError(f"Unknown or empty local tool group: {group}")
    server = Server(f"mobyai-{group}")

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description=definition.description,
                     inputSchema=definition.arguments.model_json_schema(),
                     annotations=ToolAnnotations(readOnlyHint=definition.read_only,
                         destructiveHint=name == "edit_file", idempotentHint=name != "edit_file",
                         openWorldHint=False))
                for name, definition in definitions.items()]

    @server.call_tool()
    async def call_tool(name, arguments):
        # Discovery and invocation enforce the same fixed capability list.
        if name not in definitions:
            raise ValueError(f"Tool is not available on this server: {name}")
        validated = definitions[name].arguments.model_validate(arguments)
        try:
            value = await asyncio.to_thread(OPERATIONS[name], workspace, **validated.model_dump())
            changed = [value["path"]] if name == "edit_file" and value.get("changed") else []
            outcome = ToolOutcome(name=name, ok=True, output=json.dumps(value), changed_files=changed)
        except Exception as exc:
            outcome = ToolOutcome(name=name, ok=False, error=str(exc))
        payload = outcome.model_dump(mode="json")
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structuredContent=payload, isError=not outcome.ok)

    return server


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("workspace", "environment"))
    args = parser.parse_args()
    workspace = Workspace(os.environ["MOBYAI_LOCAL_WORKSPACE"],
                          extra_allowed=json.loads(os.environ.get("MOBYAI_LOCAL_EXTRA_PATHS", "[]")))
    server = create_server(args.group, workspace,
                           read_only=os.environ.get("MOBYAI_LOCAL_READ_ONLY") == "1")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

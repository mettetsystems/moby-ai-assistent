"""Fixed local stdio MCP server. The harness owns all isolation settings."""
import os
from mcp.server.fastmcp import FastMCP
from mobyai.execution.workspace import Workspace
from .policy import SandboxConfig
from .backend import run
server = FastMCP("MobyAI offline sandbox")

@server.tool()
async def sandbox_execute(command: list[str], cwd: str = ".", timeout_seconds: int = 600) -> dict:
    """Run in an ephemeral offline container. Images/mounts/network are not model inputs."""
    config = SandboxConfig.model_validate_json(os.environ["MOBYAI_SANDBOX_POLICY"])
    if not config.enabled or not 1 <= timeout_seconds <= int(os.environ["MOBYAI_SANDBOX_TIMEOUT"]):
        raise ValueError("Sandbox disabled or requested deadline exceeds approved policy")
    result = await run(config, Workspace(os.environ["MOBYAI_SANDBOX_WORKSPACE"]),
        command, cwd, timeout_seconds, int(os.environ["MOBYAI_SANDBOX_OUTPUT_LIMIT"]), name=os.environ["MOBYAI_SANDBOX_NAME"])
    return result.model_dump()

@server.tool()
async def sandbox_prepare() -> dict:
    """Build/reuse the harness-approved profile. Accepts no model-supplied recipe."""
    from .builder import ensure
    config = SandboxConfig.model_validate_json(os.environ["MOBYAI_SANDBOX_POLICY"])
    try:
        return await ensure(config, int(os.environ["MOBYAI_SANDBOX_TIMEOUT"]), os.environ["MOBYAI_SANDBOX_NAME"])
    except TimeoutError:
        return {"timed_out":True}

if __name__ == "__main__":
    server.run(transport="stdio")

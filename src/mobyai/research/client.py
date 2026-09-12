"""Fixed local MCP bridge: models cannot choose arbitrary server commands."""
import asyncio
import json
import os
import sys

async def research_search(arguments):
    return await call_research('research_search',arguments)

async def research_lookup(arguments):
    return await call_research('research_lookup',arguments)

async def call_research(tool,arguments):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        return {'status':'unavailable', 'results':[], 'note':'Install MobyAI with the research extra: pip install -e ".[research]"'}
    # Only explicitly needed environment values reach the fixed local server.
    env = {key: os.environ[key] for key in ('HOME','PATH','SSL_CERT_FILE','SSL_CERT_DIR',
        'BRAVE_SEARCH_API_KEY','SEMANTIC_SCHOLAR_API_KEY','MOBYAI_RESEARCH_CACHE',
        'MOBYAI_RESEARCH_CACHE_FRESH_DAYS','MOBYAI_RESEARCH_CACHE_IDLE_WEEKS',
        'MOBYAI_RESEARCH_CACHE_MAX_AGE_WEEKS','MOBYAI_RESEARCH_CACHE_MAX_ENTRIES') if key in os.environ}
    params = StdioServerParameters(command=sys.executable, args=['-m','mobyai.research.server'], env=env)
    try:
        async with asyncio.timeout(60):
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
                    if result.isError:
                        return {'status':'invalid_request', 'results':[], 'note':'Use query/error strings of 3–240 characters and a supported source.'}
                    return json.loads(next(c.text for c in result.content if c.type == 'text'))
    except Exception as exc:
        return {'status':'unavailable','results':[], 'error':type(exc).__name__}

"""Run with python -m mobyai.research.server; local stdio MCP, no listening port."""
import logging

from mcp.server.fastmcp import FastMCP
from mobyai.research.search import ResearchSearch

server = FastMCP('MobyAI targeted research')
searcher = ResearchSearch()

@server.tool()
async def research_search(query: str, error: str, source: str = 'dblp', force_refresh: bool = False) -> dict:
    """Search a concise public error/concept plus framework/version. Sources: dblp,
    semantic_scholar, zenodo, web, google_scholar, papers_with_code, citeseerx,
    baidu_scholar, scielo. Inspect local evidence first. No logs, secrets, or code.
    Three uncached searches per error/day; ten seconds between requests; five hits.
    Web/site search needs BRAVE_SEARCH_API_KEY, otherwise returns a manual link.
    Cache matches include original query and age. Similar matches are candidate
    evidence, not a new search. force_refresh bypasses cache only, never API limits.
    Cite URLs, distinguish metadata from full text, and test any suggested fix.
    """
    return await searcher.search(query, error, source,force_refresh)

@server.tool()
async def research_lookup(query: str) -> dict:
    """Local-only BM25/text-vector lookup. Never calls web APIs."""
    if not 3 <= len(query) <= 12000:
        raise ValueError('Provide 3 to 12000 characters of local diagnostic evidence')
    return searcher.cache.lookup(query)

if __name__ == '__main__':
    logging.getLogger('httpx').setLevel(logging.WARNING)
    server.run(transport='stdio')

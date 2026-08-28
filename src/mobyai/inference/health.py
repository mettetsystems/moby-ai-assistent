"""HTTP helpers for OpenAI-compatible local servers."""

from __future__ import annotations

import httpx


def join_url(base: str, path: str) -> str:
    root = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    if root.endswith("/v1") and path.startswith("/v1/"):
        return root + path[3:]
    return root + path


async def openai_healthcheck(client: httpx.AsyncClient, base_url: str) -> bool:
    for path in ("/v1/models", "/health", "/v1/health"):
        try:
            response = await client.get(join_url(base_url, path))
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            return True
    return False

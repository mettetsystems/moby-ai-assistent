"""Pinned artifacts only; no package installer or project code runs online."""
import hashlib
from urllib.parse import urlsplit
import httpx

HOSTS = frozenset({"files.pythonhosted.org", "registry.npmjs.org"})
MAX_BYTES = 100 * 1024 * 1024

def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS or
        parsed.port not in (None, 443) or parsed.username or parsed.password or
        parsed.query or parsed.fragment or not parsed.path.endswith((".whl", ".tgz"))):
        raise ValueError("Only direct HTTPS wheels/npm tarballs on approved public registries are allowed")

async def prepare(artifacts, destination, *, transport=None):
    for artifact in artifacts:
        validate_url(artifact.url)
        if not artifact.filename.endswith((".whl", ".tgz")):
            raise ValueError("Dependency filename must be a wheel or npm tarball")
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                timeout=30, transport=transport) as client:
        for artifact in artifacts:
            path = destination / artifact.filename
            digest = hashlib.sha256()
            size = 0
            try:
                async with client.stream("GET", artifact.url) as response:
                    if response.status_code != 200:
                        raise ValueError(f"Dependency download returned HTTP {response.status_code}")
                    with path.open("xb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise ValueError("Dependency exceeds 100 MiB limit")
                            digest.update(chunk)
                            output.write(chunk)
                if digest.hexdigest() != artifact.sha256:
                    raise ValueError("Dependency SHA-256 mismatch")
            except BaseException:
                path.unlink(missing_ok=True)
                raise

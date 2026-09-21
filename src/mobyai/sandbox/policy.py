from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,199}$")
    url: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    image: str = ""
    auto_prepare: bool = False
    profile: Literal["python-qt-node"] = "python-qt-node"
    network: Literal["dependencies_only"] = "dependencies_only"
    memory_mb: int = Field(default=4096, ge=128, le=65536)
    cpus: int = Field(default=2, ge=1, le=64)
    pids: int = Field(default=256, ge=16, le=4096)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_policy(self):
        if self.enabled and not (self.auto_prepare and not self.image) and not re.fullmatch(r"(?:[A-Za-z0-9./:_-]+@)?sha256:[a-f0-9]{64}", self.image):
            raise ValueError("Sandbox requires a locally installed sha256 image ID or digest")
        if len({a.filename for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("Dependency filenames must be unique")
        return self

_current = ContextVar("mobyai_sandbox", default=None)
def current():
    return _current.get()

@contextmanager
def activate(config):
    token = _current.set(config if config.enabled else None)
    try:
        yield
    finally:
        _current.reset(token)


def guidance():
    config = current()
    if config is None:
        return "Execution backend: host commands with existing policy restrictions. Do not assume sandboxing is active."
    return ("Execution backend: offline rootless Podman through a harness-owned MCP server. "
            "Image: " + config.image + ". Host preflight tools may not exist in this image. "
            "Working tree: /workspace; approved artifacts: /dependencies. "
            "Only project files persist between commands. No host sudo, daemon sockets or network. "
            "Approved artifact filenames: " + ", ".join(a.filename for a in config.artifacts))

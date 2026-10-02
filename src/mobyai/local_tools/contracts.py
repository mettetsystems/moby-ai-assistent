"""One tool registry shared by worker prompts, validation and MCP discovery."""
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FileInfoArguments(Arguments):
    path: str = Field(min_length=1)


class EditFileArguments(FileInfoArguments):
    old_text: str = Field(min_length=1)
    new_text: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class PythonEnvironmentArguments(Arguments):
    path: str = Field(default=".venv", min_length=1)
    packages: list[StrictStr] = Field(default_factory=list, max_length=100)


class NodeProjectArguments(Arguments):
    path: str = Field(default=".", min_length=1)


@dataclass(frozen=True)
class Definition:
    group: str
    arguments: type[Arguments]
    description: str
    read_only: bool = True


TOOLS = {
    "file_info": Definition("workspace", FileInfoArguments,
        "Inspect a path, including missing paths, directories and symlinks; return a hash for small files."),
    "create_directory": Definition("workspace", FileInfoArguments,
        "Create a project directory and missing parents; succeeds if it already exists.", False),
    "edit_file": Definition("workspace", EditFileArguments,
        "Replace exactly one literal old_text occurrence with new_text. Read first; use the read's SHA256 "
        "to reject stale edits. Ambiguous or absent text never changes the file. Prefer this after a failed diff.", False),
    "inspect_python_environment": Definition("environment", PythonEnvironmentArguments,
        "Read venv configuration, interpreter entry and installed distribution versions from disk. "
        "Optional packages filters distribution names. Does not execute Python or prove imports/sandbox readiness."),
    "inspect_node_project": Definition("environment", NodeProjectArguments,
        "Read package.json scripts, declared dependencies, lockfile presence, installed versions and peer "
        "requirements. Use before npm installation or tests. Does not install or resolve dependency conflicts."),
}

READ_ONLY_TOOLS = frozenset(name for name, definition in TOOLS.items() if definition.read_only)

"""Worker structured output contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkerToolName(StrEnum):
    RESEARCH_LOOKUP = "research_lookup"
    RESEARCH_SEARCH = "research_search"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    APPLY_PATCH = "apply_patch"
    LIST_DIRECTORY = "list_directory"
    SEARCH_CODE = "search_code"
    GLOB = "glob"
    FIND_FILES = "find_files"
    RUN_TEST = "run_test"
    RUN_COMMAND = "run_command"
    FILE_INFO = "file_info"
    CREATE_DIRECTORY = "create_directory"
    EDIT_FILE = "edit_file"
    INSPECT_PYTHON_ENVIRONMENT = "inspect_python_environment"
    INSPECT_NODE_PROJECT = "inspect_node_project"


class WorkerToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkerToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class WorkerResult(BaseModel):
    reference_urls_used: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    tool_calls: list[WorkerToolCall] = Field(default_factory=list)
    done: bool = False
    changed_files: list[str] = Field(default_factory=list)

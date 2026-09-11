"""Worker context payload. Built by ContextPacker, not by the model."""

from __future__ import annotations

from enum import StrEnum
import json

from pydantic import BaseModel, ConfigDict, Field

from mobyai.context.log_reducer import ReducedLog


class FileRole(StrEnum):
    TARGET = "target"
    TEST = "test"
    REQUIRED_CONTEXT = "required_context"
    IMPORT = "import"


class PackedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str = ""
    role: FileRole
    truncated: bool = False
    missing: bool = False
    from_contract: bool = False


class WorkerContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    objective: str
    instruction: str
    architecture_summary: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    contract: dict[str, list[str]] = Field(default_factory=dict)
    dependency_contracts: list[dict[str, object]] = Field(default_factory=list)
    files: list[PackedFile] = Field(default_factory=list)
    reduced_error: ReducedLog | None = None
    remediation_instruction: str | None = None
    omitted_paths: list[str] = Field(default_factory=list)
    token_budget: int = Field(ge=1)
    token_count: int = Field(ge=0)

    def paths_for(self, role: FileRole) -> list[str]:
        return [item.path for item in self.files if item.role == role]

    def render(self) -> str:
        sections: list[str] = [
            f"# Task {self.task_id}",
            f"## Objective\n{self.objective}",
        ]
        if self.contract:
            sections.append("## Task contract (exact paths)\n" + json.dumps(self.contract))
        if self.dependency_contracts:
            sections.append("## Dependency contracts (not implementation instructions)\n" + json.dumps(self.dependency_contracts))
        if self.architecture_summary:
            sections.append(f"## Architecture\n{self.architecture_summary}")
        sections.append(f"## Instruction\n{self.instruction}")
        if self.remediation_instruction:
            sections.append(
                f"## Remediation instruction\n{self.remediation_instruction}"
            )
        if self.acceptance_criteria:
            bullets = "\n".join(f"- {item}" for item in self.acceptance_criteria)
            sections.append(f"## Acceptance criteria\n{bullets}")
        if self.test_commands:
            bullets = "\n".join(f"- `{item}`" for item in self.test_commands)
            sections.append(f"## Test commands\n{bullets}")
        if self.constraints:
            bullets = "\n".join(f"- {item}" for item in self.constraints)
            sections.append(f"## Constraints\n{bullets}")
        if self.reduced_error is not None:
            sections.append(
                "## Previous attempt error\n" + self.reduced_error.render()
            )
        for packed in self.files:
            flags: list[str] = []
            if packed.missing:
                flags.append("missing")
            if packed.truncated:
                flags.append("truncated")
            if packed.from_contract:
                flags.append("contract")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            body = packed.content
            sections.append(
                f"## File {packed.path} ({packed.role.value}){suffix}\n"
                f"```\n{body}\n```"
            )
        if self.omitted_paths:
            bullets = "\n".join(f"- {path}" for path in self.omitted_paths)
            sections.append(f"## Omitted from context\n{bullets}")
        return "\n\n".join(sections)

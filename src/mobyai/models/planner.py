"""Planner structured output contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mobyai.models.task import Task


class PlannerTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    target_files: list[str] = Field(default_factory=list)
    required_context: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)

    def to_task(self) -> Task:
        return Task(
            task_id=self.task_id,
            name=self.name,
            description=self.description,
            target_files=list(self.target_files),
            required_context=list(self.required_context),
            dependencies=list(self.dependencies),
            acceptance_criteria=list(self.acceptance_criteria),
            test_files=list(self.test_files),
            test_commands=list(self.test_commands),
        )


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str = Field(min_length=1)
    architecture_summary: str = Field(min_length=1)
    review_influence: list[str] = Field(default_factory=list)
    rationale_summary: str = ""
    directory_structure: list[str] = Field(default_factory=list)
    tasks: list[PlannerTaskSpec]
    documentation_requirements: list[str] = Field(default_factory=list)


class TaskSupersede(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    replacements: list[PlannerTaskSpec] = Field(min_length=1)


class DAGPatch(BaseModel):
    """Branch-local DAG edit. The Planner must not emit a full new plan."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    rationale_summary: str = ""
    architecture_summary: str | None = None
    directory_structure: list[str] = Field(default_factory=list)
    supersede: list[TaskSupersede] = Field(min_length=1)
    add_tasks: list[PlannerTaskSpec] = Field(default_factory=list)


class PlannerInspectToolName(StrEnum):
    RESEARCH_LOOKUP = "research_lookup"
    RESEARCH_SEARCH = "research_search"
    """Read-only tools the Planner may request. Writes stay on the Worker path."""

    READ_FILE = "read_file"
    LIST_DIRECTORY = "list_directory"
    SEARCH_CODE = "search_code"
    GLOB = "glob"
    FIND_FILES = "find_files"


class PlannerInspectCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: PlannerInspectToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class PlannerTurn(BaseModel):
    """One Planner generate: read-only inspect, or a full plan."""

    model_config = ConfigDict(extra="forbid")

    inspect_calls: list[PlannerInspectCall] = Field(default_factory=list)
    plan: PlannerOutput | None = None

    @model_validator(mode="before")
    @classmethod
    def lift_flat_plan(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "plan" in data:
            return data
        inspect = data.get("inspect_calls")
        has_inspect = isinstance(inspect, list) and len(inspect) > 0
        if has_inspect and "tasks" not in data:
            return data
        if "tasks" in data and "project_name" in data:
            rest = {key: value for key, value in data.items() if key != "inspect_calls"}
            if has_inspect:
                return {"inspect_calls": inspect, "plan": None}
            return {"inspect_calls": [], "plan": rest}
        return data

    @model_validator(mode="after")
    def require_inspect_or_plan(self) -> PlannerTurn:
        if not self.inspect_calls and self.plan is None:
            raise ValueError("PlannerTurn requires inspect_calls or a plan")
        return self


class PlannerPatchTurn(BaseModel):
    """One replan generate: read-only inspect, or a DAG patch."""

    model_config = ConfigDict(extra="forbid")

    inspect_calls: list[PlannerInspectCall] = Field(default_factory=list)
    patch: DAGPatch | None = None

    @model_validator(mode="before")
    @classmethod
    def lift_flat_patch(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "patch" in data:
            return data
        inspect = data.get("inspect_calls")
        has_inspect = isinstance(inspect, list) and len(inspect) > 0
        if has_inspect and "supersede" not in data:
            return data
        if "supersede" in data and "summary" in data:
            rest = {key: value for key, value in data.items() if key != "inspect_calls"}
            if has_inspect:
                return {"inspect_calls": inspect, "patch": None}
            return {"inspect_calls": [], "patch": rest}
        return data

    @model_validator(mode="after")
    def require_inspect_or_patch(self) -> PlannerPatchTurn:
        if not self.inspect_calls and self.patch is None:
            raise ValueError("PlannerPatchTurn requires inspect_calls or a patch")
        return self

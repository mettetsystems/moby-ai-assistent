"""Planner structured output contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
    architecture_summary: str | None = None
    directory_structure: list[str] = Field(default_factory=list)
    supersede: list[TaskSupersede] = Field(min_length=1)
    add_tasks: list[PlannerTaskSpec] = Field(default_factory=list)

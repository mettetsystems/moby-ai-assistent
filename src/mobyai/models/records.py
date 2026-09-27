"""Versioned evidence records. These describe observations, never learned rules."""
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

Completeness = Literal['complete', 'partial', 'unknown']


class RecordModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ArtifactRef(RecordModel):
    path: str
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)
    media_type: str = 'application/json'
    truncated: bool = False


class CaptureEntry(RecordModel):
    id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    timestamp: AwareDatetime
    scope: Literal['run', 'step', 'plan']
    kind: str
    actor: str
    step_id: UUID | None = None
    correlation_id: UUID | None = None
    parent_revision_id: UUID | None = None
    reason: str | None = None
    payload: ArtifactRef


class BaseRecord(RecordModel):
    schema_version: Literal[1] = 1
    id: UUID
    project_id: str
    run_id: UUID
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completeness: Completeness = 'complete'
    warnings: list[str] = Field(default_factory=list)

    @field_validator('created_at', 'updated_at')
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class RunRecord(BaseRecord):
    record_type: Literal['RunRecord'] = 'RunRecord'
    project_created_at: AwareDatetime
    original_prompt: ArtifactRef
    baseline: ArtifactRef
    state: str
    final_result: str | None = None
    plan_id: UUID
    step_ids: list[UUID] = Field(default_factory=list)
    history: list[CaptureEntry] = Field(default_factory=list)


class StepRunRecord(BaseRecord):
    record_type: Literal['StepRunRecord'] = 'StepRunRecord'
    task_id: str
    attempt_number: int = Field(ge=1)
    attempt_id: int | None = None
    plan_revision_id: UUID | None = None
    task_specification: ArtifactRef
    initial_prompt: ArtifactRef | None = None
    outcome: Literal['in_progress', 'pass', 'fail', 'error', 'timeout', 'interrupted'] = 'in_progress'
    finished_at: AwareDatetime | None = None
    history: list[CaptureEntry] = Field(default_factory=list)


class PlanRecord(BaseRecord):
    record_type: Literal['PlanRecord'] = 'PlanRecord'
    original_prompt: ArtifactRef
    active_revision_id: UUID | None = None
    history: list[CaptureEntry] = Field(default_factory=list)


class RecordBundle(RecordModel):
    run: RunRecord
    plan: PlanRecord
    steps: list[StepRunRecord]
    artifacts: list[ArtifactRef]

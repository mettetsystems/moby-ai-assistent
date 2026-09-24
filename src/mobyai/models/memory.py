"""Typed memory nodes. The harness writes them; models only read the working set."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MemoryKind(StrEnum):
    HYPOTHESIS = "hypothesis"
    OUTCOME = "outcome"
    HANDOFF = "handoff"
    CLAIM = "claim"
    ARTIFACT_REF = "artifact_ref"


class MemoryLayer(StrEnum):
    CORE = "core"
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class MemoryEdgeKind(StrEnum):
    DERIVED_FROM = "derived_from"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"


class MemoryNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    kind: MemoryKind
    layer: MemoryLayer
    name: str = Field(min_length=1)
    body: str
    fingerprint: str | None = None
    asserted_at: datetime
    retracted_at: datetime | None = None


class MemoryEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    kind: MemoryEdgeKind
    asserted_at: datetime
    retracted_at: datetime | None = None

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ScopeFields(BaseModel):
    workspace_id: UUID | None = None
    project_ref: str | None = Field(default=None, max_length=300)
    task_ref: str | None = Field(default=None, max_length=300)
    run_ref: str | None = Field(default=None, max_length=300)


class EpisodeCreate(ScopeFields):
    content: str = Field(min_length=1, max_length=2_000_000)
    source_type: str = Field(default="explicit", max_length=64)
    source_uri: str = Field(default="", max_length=1000)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EpisodeRead(ORMModel):
    id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    project_ref: str | None
    task_ref: str | None
    run_ref: str | None
    source_type: str
    source_uri: str
    content: str
    content_hash: str
    sensitivity: str
    occurred_at: datetime
    enrichment_status: str
    enrichment_error: str | None
    extra: dict[str, Any]
    created_at: datetime


class MemoryCreate(ScopeFields):
    subject: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    memory_type: str = Field(default="fact", max_length=64)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    confidence: float = Field(default=1.0, ge=0, le=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("valid_until")
    @classmethod
    def validate_valid_until(cls, value: datetime | None, info):  # type: ignore[no-untyped-def]
        start = info.data.get("valid_from")
        if value is not None and start is not None and value <= start:
            raise ValueError("valid_until must be after valid_from")
        return value


class MemoryUpdate(BaseModel):
    subject: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    memory_type: str | None = Field(default=None, max_length=64)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    valid_until: datetime | None = None
    metadata: dict[str, Any] | None = None


class MemoryRead(ORMModel):
    id: UUID
    episode_id: UUID | None
    organization_id: UUID
    workspace_id: UUID | None
    project_ref: str | None
    task_ref: str | None
    run_ref: str | None
    subject: str
    content: str
    memory_type: str
    sensitivity: str
    confidence: float
    status: str
    valid_from: datetime
    valid_until: datetime | None
    embedding_model: str | None
    metadata_: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class SearchRequest(ScopeFields):
    query: str = Field(min_length=1, max_length=10_000)
    limit: int = Field(default=10, ge=1, le=100)
    memory_types: list[str] = Field(default_factory=list)
    sensitivity: list[str] = Field(default_factory=list)
    temporal_as_of: datetime | None = None
    include_graph: bool = True
    include_content: bool = True


class SearchHit(BaseModel):
    memory_id: UUID | None = None
    kind: Literal["memory", "relationship"]
    subject: str
    content: str
    score: float
    backends: list[str]
    source_episode_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    degraded: list[str] = Field(default_factory=list)


class ContextRequest(SearchRequest):
    max_characters: int = Field(default=12_000, ge=500, le=200_000)


class ContextResponse(BaseModel):
    context: str
    citations: list[dict[str, Any]]
    degraded: list[str] = Field(default_factory=list)


class EntityInput(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    entity_type: str = Field(default="concept", max_length=100)
    description: str = Field(default="", max_length=10_000)
    confidence: float = Field(default=1.0, ge=0, le=1)


class RelationshipCreate(BaseModel):
    workspace_id: UUID | None = None
    source: EntityInput
    relation_type: str = Field(min_length=1, max_length=100)
    target: EntityInput
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    source_memory_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_interval(self):  # type: ignore[no-untyped-def]
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        return self


class RelationshipRead(BaseModel):
    id: UUID
    source_entity_id: UUID
    source_name: str
    source_type: str
    relation_type: str
    sensitivity: str
    target_entity_id: UUID
    target_name: str
    target_type: str
    source_memory_id: UUID | None
    confidence: float
    valid_from: datetime
    valid_until: datetime | None


class GraphQuery(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    workspace_id: UUID | None = None
    hops: int = Field(default=2, ge=1, le=5)
    limit: int = Field(default=50, ge=1, le=200)
    temporal_as_of: datetime | None = None


class WorkingMemoryCreate(BaseModel):
    task_id: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=50_000)
    workspace_id: UUID | None = None
    resource_type: str | None = Field(default=None, max_length=100)
    resource_ref: str | None = Field(default=None, max_length=500)
    expires_in_seconds: int = Field(default=7200, ge=60, le=604800)
    acquire_lock: bool = False


class WorkingMemoryUpdate(BaseModel):
    progress: dict[str, Any] | None = None
    expires_in_seconds: int | None = Field(default=None, ge=60, le=604800)


class WorkingMemoryFinish(BaseModel):
    consolidation_notes: str = Field(default="", max_length=100_000)


class WorkingMemoryRead(ORMModel):
    id: UUID
    task_id: str
    description: str
    status: str
    progress: dict[str, Any]
    resource_type: str | None
    resource_ref: str | None
    expires_at: datetime
    completed_at: datetime | None
    consolidation_notes: str | None


class LockAcquire(BaseModel):
    workspace_id: UUID | None = None
    working_memory_id: UUID | None = None
    resource_type: str = Field(min_length=1, max_length=100)
    resource_ref: str = Field(min_length=1, max_length=500)
    ttl_seconds: int = Field(default=900, ge=10, le=86400)


class LockRenew(BaseModel):
    token: str = Field(min_length=20)
    fence: int = Field(ge=1)
    ttl_seconds: int = Field(default=900, ge=10, le=86400)


class LockRelease(BaseModel):
    token: str = Field(min_length=20)
    fence: int = Field(ge=1)


class LockRead(BaseModel):
    id: UUID
    resource_type: str
    resource_ref: str
    fence: int
    expires_at: datetime
    token: str | None = None


class WorkingMemoryCreateRead(WorkingMemoryRead):
    lock: LockRead | None = None


class WorkspaceExport(BaseModel):
    ref: UUID
    name: str


class MemoryExport(BaseModel):
    ref: UUID
    workspace_ref: UUID | None = None
    project_ref: str | None = None
    task_ref: str | None = None
    run_ref: str | None = None
    subject: str
    content: str
    memory_type: str
    sensitivity: Literal["public", "internal", "confidential", "restricted"]
    confidence: float
    valid_from: datetime
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelationshipExport(BaseModel):
    workspace_ref: UUID | None = None
    source: EntityInput
    relation_type: str
    target: EntityInput
    sensitivity: Literal["public", "internal", "confidential", "restricted"]
    source_memory_ref: UUID | None = None
    confidence: float
    valid_from: datetime
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExportBundle(BaseModel):
    format_version: Literal["albert-export-v1"] = "albert-export-v1"
    exported_at: datetime
    organization_name: str
    workspaces: list[WorkspaceExport]
    memories: list[MemoryExport]
    relationships: list[RelationshipExport]


class ImportRequest(BaseModel):
    bundle: ExportBundle
    workspace_id: UUID | None = None


class ImportResult(BaseModel):
    memories_created: int
    relationships_created: int
    workspaces_created: int


class HealthResponse(BaseModel):
    status: str
    database: str
    version: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    classifier_provider: str

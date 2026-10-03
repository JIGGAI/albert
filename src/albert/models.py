from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from albert.config import get_settings
from albert.db import Base, VectorOrJSON


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))


class Principal(Base, TimestampMixin):
    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    principal_type: Mapped[str] = mapped_column(String(32), default="agent")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class APIKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    prefix: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    name: Mapped[str] = mapped_column(String(200), default="default")
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    principal: Mapped[Principal] = relationship()


class Episode(Base, TimestampMixin):
    __tablename__ = "episodes"
    __table_args__ = (
        Index(
            "uq_episodes_scope_content_source",
            "organization_id",
            "workspace_id",
            "owner_principal_id",
            "content_hash",
            "source_uri",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_episodes_scope", "organization_id", "workspace_id", "project_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    project_ref: Mapped[str | None] = mapped_column(String(300), index=True)
    task_ref: Mapped[str | None] = mapped_column(String(300), index=True)
    run_ref: Mapped[str | None] = mapped_column(String(300), index=True)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(index=True)
    source_type: Mapped[str] = mapped_column(String(64), default="explicit")
    source_uri: Mapped[str] = mapped_column(String(1000), default="")
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="internal")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    enrichment_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    enrichment_error: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Memory(Base, TimestampMixin):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_scope", "organization_id", "workspace_id", "project_ref"),
        Index("ix_memories_type", "organization_id", "memory_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"), index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    project_ref: Mapped[str | None] = mapped_column(String(300), index=True)
    task_ref: Mapped[str | None] = mapped_column(String(300), index=True)
    run_ref: Mapped[str | None] = mapped_column(String(300), index=True)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(index=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text)
    memory_type: Mapped[str] = mapped_column(String(64), default="fact")
    sensitivity: Mapped[str] = mapped_column(String(32), default="internal")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding: Mapped[list[float] | None] = mapped_column(
        VectorOrJSON(get_settings().embedding_dimensions)
    )
    embedding_model: Mapped[str | None] = mapped_column(String(300))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class Entity(Base, TimestampMixin):
    __tablename__ = "entities"
    __table_args__ = (
        Index(
            "uq_entities_scope_name_type",
            "organization_id",
            "workspace_id",
            "canonical_name",
            "entity_type",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_entities_scope", "organization_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    canonical_name: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str] = mapped_column(String(500))
    entity_type: Mapped[str] = mapped_column(String(100), default="concept")
    description: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class Relationship(Base, TimestampMixin):
    __tablename__ = "relationships"
    __table_args__ = (
        Index("ix_relationships_scope", "organization_id", "workspace_id"),
        Index("ix_relationships_nodes", "source_entity_id", "target_entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(100), index=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="internal", index=True)
    source_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), index=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    source: Mapped[Entity] = relationship(foreign_keys=[source_entity_id])
    target: Mapped[Entity] = relationship(foreign_keys=[target_entity_id])


class WorkingMemory(Base, TimestampMixin):
    __tablename__ = "working_memories"
    __table_args__ = (UniqueConstraint("organization_id", "task_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(index=True)
    task_id: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_ref: Mapped[str | None] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consolidation_notes: Mapped[str | None] = mapped_column(Text)


class ResourceLock(Base, TimestampMixin):
    __tablename__ = "resource_locks"
    __table_args__ = (UniqueConstraint("organization_id", "resource_type", "resource_ref"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(index=True)
    working_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("working_memories.id", ondelete="CASCADE"), index=True
    )
    resource_type: Mapped[str] = mapped_column(String(100))
    resource_ref: Mapped[str] = mapped_column(String(500))
    fence: Mapped[int] = mapped_column(Integer, default=1)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "available_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    job_type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_timeline", "organization_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

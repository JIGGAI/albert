"""Initial Albert schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _indexes(table: str, definitions: list[tuple[str, list[str], bool]]) -> None:
    for name, columns, unique in definitions:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    connection = op.get_bind()
    embedding_type = VECTOR(384) if connection.dialect.name == "postgresql" else sa.JSON()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("name", name="uq_organizations_name"),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("organization_id", "name", name="uq_workspaces_org_name"),
    )
    _indexes("workspaces", [("ix_workspaces_organization_id", ["organization_id"], False)])

    op.create_table(
        "principals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    _indexes(
        "principals",
        [
            ("ix_principals_organization_id", ["organization_id"], False),
            ("ix_principals_workspace_id", ["workspace_id"], False),
        ],
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "principal_id",
            sa.Uuid(),
            sa.ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prefix", sa.String(24), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    _indexes(
        "api_keys",
        [
            ("ix_api_keys_principal_id", ["principal_id"], False),
            ("ix_api_keys_prefix", ["prefix"], True),
        ],
    )

    op.create_table(
        "episodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("project_ref", sa.String(300), nullable=True),
        sa.Column("task_ref", sa.String(300), nullable=True),
        sa.Column("run_ref", sa.String(300), nullable=True),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_uri", sa.String(1000), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enrichment_status", sa.String(32), nullable=False),
        sa.Column("enrichment_error", sa.Text(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "organization_id", "content_hash", "source_uri", name="uq_episodes_org_content_source"
        ),
    )
    _indexes(
        "episodes",
        [
            ("ix_episodes_organization_id", ["organization_id"], False),
            ("ix_episodes_workspace_id", ["workspace_id"], False),
            ("ix_episodes_project_ref", ["project_ref"], False),
            ("ix_episodes_task_ref", ["task_ref"], False),
            ("ix_episodes_run_ref", ["run_ref"], False),
            ("ix_episodes_owner_principal_id", ["owner_principal_id"], False),
            ("ix_episodes_content_hash", ["content_hash"], False),
            ("ix_episodes_enrichment_status", ["enrichment_status"], False),
            ("ix_episodes_scope", ["organization_id", "workspace_id", "project_ref"], False),
        ],
    )

    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("project_ref", sa.String(300), nullable=True),
        sa.Column("task_ref", sa.String(300), nullable=True),
        sa.Column("run_ref", sa.String(300), nullable=True),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.String(64), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", embedding_type, nullable=True),
        sa.Column("embedding_model", sa.String(300), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamps(),
    )
    _indexes(
        "memories",
        [
            ("ix_memories_episode_id", ["episode_id"], False),
            ("ix_memories_organization_id", ["organization_id"], False),
            ("ix_memories_workspace_id", ["workspace_id"], False),
            ("ix_memories_project_ref", ["project_ref"], False),
            ("ix_memories_task_ref", ["task_ref"], False),
            ("ix_memories_run_ref", ["run_ref"], False),
            ("ix_memories_owner_principal_id", ["owner_principal_id"], False),
            ("ix_memories_status", ["status"], False),
            ("ix_memories_scope", ["organization_id", "workspace_id", "project_ref"], False),
            ("ix_memories_type", ["organization_id", "memory_type"], False),
        ],
    )

    op.create_table(
        "entities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_name", sa.String(500), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "canonical_name",
            "entity_type",
            name="uq_entities_org_workspace_name_type",
        ),
    )
    _indexes(
        "entities",
        [
            ("ix_entities_organization_id", ["organization_id"], False),
            ("ix_entities_workspace_id", ["workspace_id"], False),
            ("ix_entities_scope", ["organization_id", "workspace_id"], False),
        ],
    )

    op.create_table(
        "relationships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column(
            "source_entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(100), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column(
            "source_memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamps(),
    )
    _indexes(
        "relationships",
        [
            ("ix_relationships_organization_id", ["organization_id"], False),
            ("ix_relationships_workspace_id", ["workspace_id"], False),
            ("ix_relationships_source_entity_id", ["source_entity_id"], False),
            ("ix_relationships_target_entity_id", ["target_entity_id"], False),
            ("ix_relationships_relation_type", ["relation_type"], False),
            ("ix_relationships_sensitivity", ["sensitivity"], False),
            ("ix_relationships_source_memory_id", ["source_memory_id"], False),
            ("ix_relationships_scope", ["organization_id", "workspace_id"], False),
            ("ix_relationships_nodes", ["source_entity_id", "target_entity_id"], False),
        ],
    )

    op.create_table(
        "working_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=True),
        sa.Column("resource_ref", sa.String(500), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consolidation_notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("organization_id", "task_id", name="uq_working_memories_org_task"),
    )
    _indexes(
        "working_memories",
        [
            ("ix_working_memories_organization_id", ["organization_id"], False),
            ("ix_working_memories_workspace_id", ["workspace_id"], False),
            ("ix_working_memories_owner_principal_id", ["owner_principal_id"], False),
            ("ix_working_memories_status", ["status"], False),
            ("ix_working_memories_expires_at", ["expires_at"], False),
        ],
    )

    op.create_table(
        "resource_locks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column(
            "working_memory_id",
            sa.Uuid(),
            sa.ForeignKey("working_memories.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_ref", sa.String(500), nullable=False),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "organization_id",
            "resource_type",
            "resource_ref",
            name="uq_resource_locks_org_resource",
        ),
    )
    _indexes(
        "resource_locks",
        [
            ("ix_resource_locks_organization_id", ["organization_id"], False),
            ("ix_resource_locks_workspace_id", ["workspace_id"], False),
            ("ix_resource_locks_owner_principal_id", ["owner_principal_id"], False),
            ("ix_resource_locks_working_memory_id", ["working_memory_id"], False),
            ("ix_resource_locks_expires_at", ["expires_at"], False),
        ],
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(200), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
    )
    _indexes(
        "jobs",
        [
            ("ix_jobs_organization_id", ["organization_id"], False),
            ("ix_jobs_job_type", ["job_type"], False),
            ("ix_jobs_status", ["status"], False),
            ("ix_jobs_claim", ["status", "available_at"], False),
        ],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=True),
        sa.Column("resource_id", sa.String(200), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _indexes(
        "audit_events",
        [
            ("ix_audit_events_organization_id", ["organization_id"], False),
            ("ix_audit_events_workspace_id", ["workspace_id"], False),
            ("ix_audit_events_principal_id", ["principal_id"], False),
            ("ix_audit_events_event_type", ["event_type"], False),
            ("ix_audit_timeline", ["organization_id", "created_at"], False),
        ],
    )

    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "CREATE INDEX ix_memories_embedding_hnsw ON memories "
                "USING hnsw (embedding vector_cosine_ops) "
                "WHERE embedding IS NOT NULL"
            )
        )
        connection.execute(
            sa.text(
                "CREATE INDEX ix_memories_fts ON memories USING gin "
                "(to_tsvector('english', coalesce(subject, '') || "
                "' ' || coalesce(content, '')))"
            )
        )


def downgrade() -> None:
    for table in (
        "audit_events",
        "jobs",
        "resource_locks",
        "working_memories",
        "relationships",
        "entities",
        "memories",
        "episodes",
        "api_keys",
        "principals",
        "workspaces",
        "organizations",
    ):
        op.drop_table(table)

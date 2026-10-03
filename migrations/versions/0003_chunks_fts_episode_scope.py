"""Chunked embeddings, stored full-text vector, per-principal episode dedupe.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

EPISODE_SCOPE_INDEX = "uq_episodes_scope_content_source"


def upgrade() -> None:
    connection = op.get_bind()
    postgres = connection.dialect.name == "postgresql"
    embedding_type = VECTOR(384) if postgres else sa.JSON()

    # Episodes: soft-delete marker and dedupe scoped to the owning principal.
    with op.batch_alter_table("episodes") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_index(EPISODE_SCOPE_INDEX, table_name="episodes")
    op.create_index(
        EPISODE_SCOPE_INDEX,
        "episodes",
        ["organization_id", "workspace_id", "owner_principal_id", "content_hash", "source_uri"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    # Memory chunks carry the embeddings from now on.
    op.create_table(
        "memory_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", embedding_type, nullable=True),
        sa.Column("embedding_model", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("memory_id", "chunk_index", name="uq_memory_chunks_memory_index"),
    )
    for name, columns in (
        ("ix_memory_chunks_memory_id", ["memory_id"]),
        ("ix_memory_chunks_organization_id", ["organization_id"]),
        ("ix_memory_chunks_workspace_id", ["workspace_id"]),
        ("ix_memory_chunks_scope_model", ["organization_id", "embedding_model"]),
    ):
        op.create_index(name, "memory_chunks", columns)

    if postgres:
        connection.execute(sa.text("DROP INDEX IF EXISTS ix_memories_embedding_hnsw"))
        connection.execute(sa.text("DROP INDEX IF EXISTS ix_memories_fts"))
        connection.execute(
            sa.text(
                "CREATE INDEX ix_memory_chunks_embedding_hnsw ON memory_chunks "
                "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
            )
        )
        connection.execute(
            sa.text(
                "ALTER TABLE memories ADD COLUMN search_vector tsvector "
                "GENERATED ALWAYS AS (to_tsvector('english'::regconfig, "
                "coalesce(subject, '') || ' ' || coalesce(content, ''))) STORED"
            )
        )
        connection.execute(
            sa.text("CREATE INDEX ix_memories_search_vector ON memories USING gin (search_vector)")
        )

    # Vectors now live on chunks; existing memories are re-embedded by
    # `albert-admin reindex-memories` (their embedding_model is cleared so the
    # default selection picks them up).
    with op.batch_alter_table("memories") as batch:
        batch.drop_column("embedding")
    op.execute(sa.text("UPDATE memories SET embedding_model = NULL"))


def downgrade() -> None:
    connection = op.get_bind()
    postgres = connection.dialect.name == "postgresql"
    embedding_type = VECTOR(384) if postgres else sa.JSON()
    with op.batch_alter_table("memories") as batch:
        batch.add_column(sa.Column("embedding", embedding_type, nullable=True))
    if postgres:
        connection.execute(sa.text("DROP INDEX IF EXISTS ix_memories_search_vector"))
        connection.execute(sa.text("ALTER TABLE memories DROP COLUMN search_vector"))
        connection.execute(
            sa.text(
                "CREATE INDEX ix_memories_embedding_hnsw ON memories "
                "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
            )
        )
        connection.execute(
            sa.text(
                "CREATE INDEX ix_memories_fts ON memories USING gin "
                "(to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(content, '')))"
            )
        )
    op.drop_table("memory_chunks")
    op.drop_index(EPISODE_SCOPE_INDEX, table_name="episodes")
    op.create_index(
        EPISODE_SCOPE_INDEX,
        "episodes",
        ["organization_id", "workspace_id", "content_hash", "source_uri"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    with op.batch_alter_table("episodes") as batch:
        batch.drop_column("deleted_at")

"""Initial Albert schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from alembic import op
from sqlalchemy import text

from albert import models  # noqa: F401
from albert.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(bind=connection)
    if connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memories_embedding_hnsw "
                "ON memories USING hnsw (embedding vector_cosine_ops) "
                "WHERE embedding IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memories_fts "
                "ON memories USING gin "
                "(to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(content, '')))"
            )
        )


def downgrade() -> None:
    connection = op.get_bind()
    Base.metadata.drop_all(bind=connection)


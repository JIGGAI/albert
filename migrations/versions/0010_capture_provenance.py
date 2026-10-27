"""Add normalized capture provenance.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("capture_sessions", "capture_events"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("provenance", sa.JSON(), nullable=True))
            batch.add_column(sa.Column("project_identity", sa.String(64), nullable=True))
            batch.add_column(sa.Column("source_commit", sa.String(128), nullable=True))
            batch.add_column(sa.Column("branch_ref", sa.String(300), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET provenance = '{{}}' WHERE provenance IS NULL"))
        with op.batch_alter_table(table) as batch:
            batch.alter_column("provenance", existing_type=sa.JSON(), nullable=False)
            for column in ("project_identity", "source_commit", "branch_ref"):
                batch.create_index(f"ix_{table}_{column}", [column])


def downgrade() -> None:
    for table in ("capture_events", "capture_sessions"):
        with op.batch_alter_table(table) as batch:
            for column in ("branch_ref", "source_commit", "project_identity"):
                batch.drop_index(f"ix_{table}_{column}")
            for column in ("branch_ref", "source_commit", "project_identity", "provenance"):
                batch.drop_column(column)

"""Flight-recorder traces.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("spans", sa.JSON(), nullable=False),
    )
    for name, columns in (
        ("ix_traces_kind", ["kind"]),
        ("ix_traces_name", ["name"]),
        ("ix_traces_status", ["status"]),
        ("ix_traces_started", ["started_at", "id"]),
        ("ix_traces_written", ["written_at", "id"]),
        ("ix_traces_org_started", ["organization_id", "started_at"]),
    ):
        op.create_index(name, "traces", columns)


def downgrade() -> None:
    op.drop_table("traces")

"""Derived memory links, recall rows, recall stats and console cursors.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column(
            "source_memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_memory_id", "target_memory_id", "kind", name="uq_memory_links_pair_kind"
        ),
    )
    for name, columns in (
        ("ix_memory_links_organization_id", ["organization_id"]),
        ("ix_memory_links_source_memory_id", ["source_memory_id"]),
        ("ix_memory_links_target_memory_id", ["target_memory_id"]),
        ("ix_memory_links_kind", ["kind"]),
        ("ix_memory_links_scope", ["organization_id", "workspace_id"]),
    ):
        op.create_index(name, "memory_links", columns)

    op.create_table(
        "memory_recalls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("query", sa.String(500), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("trace_id", "memory_id", name="uq_memory_recalls_trace_memory"),
    )
    for name, columns in (
        ("ix_memory_recalls_organization_id", ["organization_id"]),
        ("ix_memory_recalls_trace_id", ["trace_id"]),
        ("ix_memory_recalls_recalled_at", ["recalled_at"]),
        ("ix_memory_recalls_memory_time", ["memory_id", "recalled_at"]),
    ):
        op.create_index(name, "memory_recalls", columns)

    op.create_table(
        "memory_stats",
        sa.Column(
            "memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("recall_count", sa.Integer(), nullable=False),
        sa.Column("last_recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_stats_organization_id", "memory_stats", ["organization_id"])

    op.create_table(
        "console_state",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("console_state", "memory_stats", "memory_recalls", "memory_links"):
        op.drop_table(table)

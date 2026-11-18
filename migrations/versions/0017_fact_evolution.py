"""Evidence-based fact evolution ledger.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fact_evolutions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("evidence_memory_id", sa.Uuid(), nullable=True),
        sa.Column("fact_memory_id", sa.Uuid(), nullable=True),
        sa.Column("result_memory_id", sa.Uuid(), nullable=True),
        sa.Column("relation", sa.String(24), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("rationale_code", sa.String(100), nullable=False),
        sa.Column("review_reason", sa.String(100), nullable=True),
        sa.Column("impact", sa.String(16), nullable=False),
        sa.Column("source_trust", sa.String(16), nullable=False),
        sa.Column("confidence_ceiling", sa.Float(), nullable=False),
        sa.Column("effective_confidence", sa.Float(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("reviewed_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["evidence_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["fact_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["result_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["reviewed_by_principal_id"], ["principals.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "principal_id",
            "idempotency_key",
            name="uq_fact_evolutions_principal_key",
        ),
    )
    op.create_index(
        "ix_fact_evolutions_scope",
        "fact_evolutions",
        ["organization_id", "workspace_id", "principal_id"],
    )
    for column in (
        "organization_id",
        "workspace_id",
        "principal_id",
        "evidence_memory_id",
        "fact_memory_id",
        "result_memory_id",
        "state",
    ):
        op.create_index(f"ix_fact_evolutions_{column}", "fact_evolutions", [column])


def downgrade() -> None:
    op.drop_table("fact_evolutions")

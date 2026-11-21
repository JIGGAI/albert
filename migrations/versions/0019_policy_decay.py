"""Policy-driven decay and forgetting state.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profile_assertions",
        sa.Column("decay_multiplier", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column(
        "profile_assertions",
        sa.Column("decay_reason", sa.String(100), nullable=False, server_default="fresh"),
    )
    op.create_table(
        "decay_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_scope", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by_principal_id", sa.Uuid(), nullable=False),
        sa.Column("rollback_of_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "workspace_scope", "version", name="uq_decay_policy_version"
        ),
    )
    op.create_index(
        "ix_decay_policies_scope",
        "decay_policies",
        ["organization_id", "workspace_id", "active"],
    )
    for column in ("organization_id", "workspace_id", "active", "created_by_principal_id"):
        op.create_index(f"ix_decay_policies_{column}", "decay_policies", [column])

    op.create_table(
        "decay_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("rule_class", sa.String(64), nullable=False),
        sa.Column("freshness_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("age_days", sa.Float(), nullable=False),
        sa.Column("score_multiplier", sa.Float(), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_id"], ["decay_policies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_id", name="uq_decay_states_memory"),
    )
    op.create_index(
        "ix_decay_states_scope",
        "decay_states",
        ["organization_id", "workspace_id", "stale"],
    )
    for column in ("memory_id", "organization_id", "workspace_id", "policy_id", "stale"):
        op.create_index(f"ix_decay_states_{column}", "decay_states", [column])


def downgrade() -> None:
    op.drop_table("decay_states")
    op.drop_table("decay_policies")
    op.drop_column("profile_assertions", "decay_reason")
    op.drop_column("profile_assertions", "decay_multiplier")

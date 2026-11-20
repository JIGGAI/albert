"""Profile administration contracts.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("profile_assertions") as batch:
        batch.add_column(sa.Column("assertion_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(
            sa.Column("decision_state", sa.String(24), nullable=False, server_default="derived")
        )
        batch.add_column(
            sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.create_index("ix_profile_assertions_assertion_key", ["assertion_key"])
    op.create_table(
        "profile_bucket_policies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_scope", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("buckets", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_principal_id"], ["principals.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "workspace_scope", "version"),
        sa.UniqueConstraint("organization_id", "workspace_scope", "idempotency_key"),
    )
    op.create_index(
        "ix_profile_bucket_policies_scope",
        "profile_bucket_policies",
        ["organization_id", "workspace_id"],
    )
    for column in ("organization_id", "workspace_id"):
        op.create_index(
            f"ix_profile_bucket_policies_{column}",
            "profile_bucket_policies",
            [column],
        )
    op.create_table(
        "profile_assertion_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("assertion_id", sa.Uuid(), nullable=False),
        sa.Column("assertion_key", sa.String(64), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prior_value", sa.Text(), nullable=False),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_principal_id"], ["principals.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "idempotency_key"),
    )
    op.create_index(
        "ix_profile_assertion_history_scope",
        "profile_assertion_history",
        ["organization_id", "principal_id"],
    )
    op.create_index(
        "ix_profile_assertion_history_assertion_key", "profile_assertion_history", ["assertion_key"]
    )
    for column in (
        "organization_id",
        "workspace_id",
        "principal_id",
        "assertion_id",
        "sensitivity",
    ):
        op.create_index(
            f"ix_profile_assertion_history_{column}",
            "profile_assertion_history",
            [column],
        )


def downgrade() -> None:
    op.drop_table("profile_assertion_history")
    op.drop_table("profile_bucket_policies")
    with op.batch_alter_table("profile_assertions") as batch:
        batch.drop_index("ix_profile_assertions_assertion_key")
        for column in ("suppressed", "decision_state", "version", "assertion_key"):
            batch.drop_column(column)

"""Materialized standing profiles.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile_materializations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_scope", sa.String(36), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("bucket_config", sa.JSON(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("assertion_count", sa.Integer(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_scope",
            "principal_id",
            name="uq_profile_materializations_scope_principal",
        ),
    )
    op.create_index(
        "ix_profile_materializations_scope",
        "profile_materializations",
        ["organization_id", "workspace_id"],
    )
    op.create_index(
        "ix_profile_materializations_principal_id",
        "profile_materializations",
        ["principal_id"],
    )
    op.create_table(
        "profile_assertions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("materialization_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trust", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["materialization_id"], ["profile_materializations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "materialization_id",
            "ordinal",
            name="uq_profile_assertions_materialization_ordinal",
        ),
    )
    op.create_index(
        "ix_profile_assertions_scope",
        "profile_assertions",
        ["organization_id", "workspace_id", "principal_id"],
    )
    op.create_index(
        "ix_profile_assertions_materialization_id",
        "profile_assertions",
        ["materialization_id"],
    )
    op.create_index("ix_profile_assertions_bucket", "profile_assertions", ["bucket"])
    op.create_index(
        "ix_profile_assertions_sensitivity", "profile_assertions", ["sensitivity"]
    )


def downgrade() -> None:
    op.drop_table("profile_assertions")
    op.drop_table("profile_materializations")

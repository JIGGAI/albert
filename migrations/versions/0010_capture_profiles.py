"""Add server-enforced automatic-capture profiles.

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
    op.create_table(
        "capture_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("scope_key", sa.String(36), nullable=False),
        sa.Column("profile_key", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("allowed_event_classes", sa.JSON(), nullable=False),
        sa.Column("metadata_allowlists", sa.JSON(), nullable=False),
        sa.Column("event_bounds", sa.JSON(), nullable=False),
        sa.Column("sensitivity_floor", sa.String(32), nullable=False),
        sa.Column("source_trust", sa.String(32), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("raw_body_policy", sa.String(16), nullable=False),
        sa.Column("deny_reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id",
            "scope_key",
            "profile_key",
            "version",
            name="uq_capture_profiles_scope_key_version",
        ),
    )
    for column in ("organization_id", "workspace_id"):
        op.create_index(f"ix_capture_profiles_{column}", "capture_profiles", [column])
    op.create_index(
        "ix_capture_profiles_scope_key",
        "capture_profiles",
        ["organization_id", "workspace_id", "profile_key"],
    )

    op.add_column(
        "capture_sessions",
        sa.Column("profile_key", sa.String(100), nullable=False, server_default="safe-default"),
    )
    op.add_column("capture_sessions", sa.Column("local_event_classes", sa.JSON()))
    op.add_column("capture_sessions", sa.Column("local_metadata_allowlists", sa.JSON()))
    op.add_column(
        "capture_events",
        sa.Column("profile_key", sa.String(100), nullable=False, server_default="safe-default"),
    )
    op.add_column(
        "capture_events",
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "capture_events",
        sa.Column("policy_decision", sa.String(16), nullable=False, server_default="allow"),
    )
    op.add_column(
        "capture_events",
        sa.Column("sensitivity", sa.String(32), nullable=False, server_default="internal"),
    )
    op.add_column(
        "capture_events",
        sa.Column("source_trust", sa.String(32), nullable=False, server_default="untrusted"),
    )
    op.add_column(
        "capture_events",
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "capture_events",
        sa.Column("body_kind", sa.String(16), nullable=False, server_default="summary"),
    )


def downgrade() -> None:
    for column in (
        "body_kind",
        "retention_days",
        "source_trust",
        "sensitivity",
        "policy_decision",
        "profile_version",
        "profile_key",
    ):
        op.drop_column("capture_events", column)
    for column in ("local_metadata_allowlists", "local_event_classes", "profile_key"):
        op.drop_column("capture_sessions", column)
    op.drop_table("capture_profiles")

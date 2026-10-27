"""Add automatic-capture session, event, and checkpoint contracts.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "capture_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column("client_session_id", sa.String(300), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_idempotency_key", sa.String(200), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "owner_principal_id", "client_session_id", name="uq_capture_sessions_principal_client"
        ),
        sa.UniqueConstraint(
            "owner_principal_id", "idempotency_key", name="uq_capture_sessions_principal_key"
        ),
    )
    op.create_index(
        "ix_capture_sessions_scope", "capture_sessions", ["organization_id", "workspace_id"]
    )
    op.create_index("ix_capture_sessions_organization_id", "capture_sessions", ["organization_id"])
    op.create_index("ix_capture_sessions_workspace_id", "capture_sessions", ["workspace_id"])
    op.create_index(
        "ix_capture_sessions_owner_principal_id", "capture_sessions", ["owner_principal_id"]
    )
    op.create_index("ix_capture_sessions_status", "capture_sessions", ["status"])

    op.create_table(
        "capture_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(16), nullable=False),
        sa.Column("event_class", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id", ondelete="SET NULL")),
        sa.Column(
            "derived_memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="SET NULL")
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("session_id", "sequence", name="uq_capture_events_session_sequence"),
        sa.UniqueConstraint(
            "session_id", "idempotency_key", name="uq_capture_events_session_key"
        ),
    )
    for column in (
        "session_id",
        "organization_id",
        "workspace_id",
        "owner_principal_id",
        "event_class",
        "episode_id",
        "derived_memory_id",
    ):
        op.create_index(f"ix_capture_events_{column}", "capture_events", [column])
    op.create_index(
        "ix_capture_events_scope", "capture_events", ["organization_id", "workspace_id"]
    )

    op.create_table(
        "capture_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column("through_sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(16), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id", ondelete="SET NULL")),
        sa.Column(
            "derived_memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="SET NULL")
        ),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "session_id", "through_sequence", name="uq_capture_checkpoints_session_sequence"
        ),
        sa.UniqueConstraint(
            "session_id", "idempotency_key", name="uq_capture_checkpoints_session_key"
        ),
    )
    for column in (
        "session_id",
        "organization_id",
        "workspace_id",
        "owner_principal_id",
        "episode_id",
        "derived_memory_id",
    ):
        op.create_index(f"ix_capture_checkpoints_{column}", "capture_checkpoints", [column])


def downgrade() -> None:
    op.drop_table("capture_checkpoints")
    op.drop_table("capture_events")
    op.drop_table("capture_sessions")

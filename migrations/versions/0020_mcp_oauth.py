"""OAuth clients and authorization codes for the MCP connector.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(80), nullable=False),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("client_uri", sa.String(500), nullable=True),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.create_table(
        "oauth_authorization_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("client_id", sa.String(500), nullable=False),
        sa.Column("redirect_uri", sa.String(2000), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(200), nullable=True),
        sa.Column("resource", sa.String(2000), nullable=True),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("credential", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_index(
        "ix_oauth_authorization_codes_client_id", "oauth_authorization_codes", ["client_id"]
    )
    op.create_index(
        "ix_oauth_authorization_codes_expires_at", "oauth_authorization_codes", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_authorization_codes_expires_at", table_name="oauth_authorization_codes")
    op.drop_index("ix_oauth_authorization_codes_client_id", table_name="oauth_authorization_codes")
    op.drop_table("oauth_authorization_codes")
    op.drop_table("oauth_clients")

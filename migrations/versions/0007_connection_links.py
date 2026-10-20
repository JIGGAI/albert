"""Single-use connection links that mint one scoped API key.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connection_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE")),
        sa.Column(
            "created_by_principal_id",
            sa.Uuid(),
            sa.ForeignKey("principals.id", ondelete="SET NULL"),
        ),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("api_key_id", sa.Uuid(), sa.ForeignKey("api_keys.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_connection_links_organization_id", "connection_links", ["organization_id"])
    op.create_index("ix_connection_links_workspace_id", "connection_links", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_connection_links_workspace_id", table_name="connection_links")
    op.drop_index("ix_connection_links_organization_id", table_name="connection_links")
    op.drop_table("connection_links")

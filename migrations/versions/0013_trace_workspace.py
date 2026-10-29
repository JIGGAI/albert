"""Record the workspace each trace ran in.

A workspace-scoped console key sees only its own workspace's traces. Existing
request traces take the workspace of the principal that made them; job traces
from before this revision stay organization-wide.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("traces", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.execute(
        """
        UPDATE traces SET workspace_id = (
            SELECT principals.workspace_id FROM principals
            WHERE principals.id = traces.principal_id
        )
        WHERE principal_id IS NOT NULL
        """
    )
    op.create_index(
        "ix_traces_org_workspace_started",
        "traces",
        ["organization_id", "workspace_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_traces_org_workspace_started", table_name="traces")
    op.drop_column("traces", "workspace_id")

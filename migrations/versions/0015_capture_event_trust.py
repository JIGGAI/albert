"""Per-event-class source trust on capture profiles.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("capture_profiles") as batch_op:
        batch_op.add_column(
            sa.Column("event_source_trust", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("capture_profiles") as batch_op:
        batch_op.drop_column("event_source_trust")

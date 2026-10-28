"""Add capture secret screening and derivative retention policy.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "capture_profiles",
        sa.Column("secret_policy", sa.String(16), nullable=False, server_default="reject"),
    )
    op.add_column(
        "capture_profiles",
        sa.Column("derivative_policy", sa.String(16), nullable=False, server_default="cascade"),
    )
    op.add_column(
        "capture_events",
        sa.Column("screening_decision", sa.String(16), nullable=False, server_default="clean"),
    )
    op.add_column(
        "capture_events",
        sa.Column("screening_reasons", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "capture_events",
        sa.Column("derivative_policy", sa.String(16), nullable=False, server_default="cascade"),
    )


def downgrade() -> None:
    for column in ("derivative_policy", "screening_reasons", "screening_decision"):
        op.drop_column("capture_events", column)
    for column in ("derivative_policy", "secret_policy"):
        op.drop_column("capture_profiles", column)

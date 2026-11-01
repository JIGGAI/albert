"""Track retry-safe capture checkpoint promotion.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("capture_checkpoints") as batch_op:
        batch_op.add_column(
            sa.Column("promotion_status", sa.String(24), nullable=False, server_default="pending")
        )
        batch_op.add_column(sa.Column("promotion_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("promotion_job_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_capture_checkpoints_promotion_job_id_jobs",
            "jobs",
            ["promotion_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_capture_checkpoints_promotion_status", "capture_checkpoints", ["promotion_status"]
    )
    op.create_index(
        "ix_capture_checkpoints_promotion_job_id", "capture_checkpoints", ["promotion_job_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_capture_checkpoints_promotion_job_id", table_name="capture_checkpoints")
    op.drop_index("ix_capture_checkpoints_promotion_status", table_name="capture_checkpoints")
    with op.batch_alter_table("capture_checkpoints") as batch_op:
        batch_op.drop_constraint(
            "fk_capture_checkpoints_promotion_job_id_jobs", type_="foreignkey"
        )
        batch_op.drop_column("promotion_job_id")
        batch_op.drop_column("promotion_error")
        batch_op.drop_column("promotion_status")

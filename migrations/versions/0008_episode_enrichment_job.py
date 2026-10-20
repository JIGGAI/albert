"""Link each episode to the job that indexes it.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Batch mode so the foreign key can be added on SQLite as well as PostgreSQL.
    with op.batch_alter_table("episodes") as batch_op:
        batch_op.add_column(sa.Column("enrichment_job_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_episodes_enrichment_job_id_jobs",
            "jobs",
            ["enrichment_job_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("episodes") as batch_op:
        batch_op.drop_constraint("fk_episodes_enrichment_job_id_jobs", type_="foreignkey")
        batch_op.drop_column("enrichment_job_id")

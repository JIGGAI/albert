"""Task-record episode reference on working memory.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Batch mode so the foreign key can be added on SQLite (copy-and-move) as
    # well as PostgreSQL, where it is a plain ALTER TABLE.
    with op.batch_alter_table("working_memories") as batch_op:
        batch_op.add_column(sa.Column("record_episode_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_working_memories_record_episode_id_episodes",
            "episodes",
            ["record_episode_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_working_memories_record_episode_id", ["record_episode_id"])


def downgrade() -> None:
    with op.batch_alter_table("working_memories") as batch_op:
        batch_op.drop_index("ix_working_memories_record_episode_id")
        batch_op.drop_constraint(
            "fk_working_memories_record_episode_id_episodes", type_="foreignkey"
        )
        batch_op.drop_column("record_episode_id")

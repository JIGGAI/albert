"""Make episode and entity uniqueness workspace-aware.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        with op.batch_alter_table("episodes") as batch:
            batch.drop_constraint("uq_episodes_org_content_source", type_="unique")
            batch.create_index(
                "uq_episodes_scope_content_source",
                ["organization_id", "workspace_id", "content_hash", "source_uri"],
                unique=True,
            )
        with op.batch_alter_table("entities") as batch:
            batch.drop_constraint("uq_entities_org_workspace_name_type", type_="unique")
            batch.create_index(
                "uq_entities_scope_name_type",
                ["organization_id", "workspace_id", "canonical_name", "entity_type"],
                unique=True,
            )
        return

    connection.execute(
        sa.text(
            """
            DO $$
            DECLARE constraint_name text;
            BEGIN
              FOR constraint_name IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'episodes'::regclass AND contype = 'u'
                  AND pg_get_constraintdef(oid) LIKE '%organization_id%content_hash%source_uri%'
              LOOP
                EXECUTE format('ALTER TABLE episodes DROP CONSTRAINT %I', constraint_name);
              END LOOP;
              FOR constraint_name IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'entities'::regclass AND contype = 'u'
                  AND pg_get_constraintdef(oid) LIKE
                    ('%organization_id%workspace_id%' || 'canonical_name%entity_type%')
              LOOP
                EXECUTE format('ALTER TABLE entities DROP CONSTRAINT %I', constraint_name);
              END LOOP;
            END $$;
            """
        )
    )
    connection.execute(
        sa.text(
            """
            WITH ranked AS (
              SELECT id,
                first_value(id) OVER (
                  PARTITION BY organization_id, workspace_id, canonical_name, entity_type
                  ORDER BY id::text
                ) AS retained_id
              FROM entities
            )
            UPDATE relationships AS relationship
            SET source_entity_id = ranked.retained_id
            FROM ranked
            WHERE relationship.source_entity_id = ranked.id
              AND ranked.id <> ranked.retained_id
            """
        )
    )
    connection.execute(
        sa.text(
            """
            WITH ranked AS (
              SELECT id,
                first_value(id) OVER (
                  PARTITION BY organization_id, workspace_id, canonical_name, entity_type
                  ORDER BY id::text
                ) AS retained_id
              FROM entities
            )
            UPDATE relationships AS relationship
            SET target_entity_id = ranked.retained_id
            FROM ranked
            WHERE relationship.target_entity_id = ranked.id
              AND ranked.id <> ranked.retained_id
            """
        )
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM entities
            WHERE id IN (
              SELECT id FROM (
                SELECT id,
                  row_number() OVER (
                    PARTITION BY organization_id, workspace_id, canonical_name, entity_type
                    ORDER BY id::text
                  ) AS duplicate_number
                FROM entities
              ) AS ranked
              WHERE duplicate_number > 1
            )
            """
        )
    )
    op.create_index(
        "uq_episodes_scope_content_source",
        "episodes",
        ["organization_id", "workspace_id", "content_hash", "source_uri"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "uq_entities_scope_name_type",
        "entities",
        ["organization_id", "workspace_id", "canonical_name", "entity_type"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        with op.batch_alter_table("entities") as batch:
            batch.drop_index("uq_entities_scope_name_type")
            batch.create_unique_constraint(
                "uq_entities_org_workspace_name_type",
                ["organization_id", "workspace_id", "canonical_name", "entity_type"],
            )
        with op.batch_alter_table("episodes") as batch:
            batch.drop_index("uq_episodes_scope_content_source")
            batch.create_unique_constraint(
                "uq_episodes_org_content_source",
                ["organization_id", "content_hash", "source_uri"],
            )
        return
    op.drop_index("uq_entities_scope_name_type", table_name="entities")
    op.drop_index("uq_episodes_scope_content_source", table_name="episodes")
    op.create_unique_constraint(
        "uq_entities_org_workspace_name_type",
        "entities",
        ["organization_id", "workspace_id", "canonical_name", "entity_type"],
    )
    op.create_unique_constraint(
        "uq_episodes_org_content_source",
        "episodes",
        ["organization_id", "content_hash", "source_uri"],
    )

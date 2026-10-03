from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from albert import models  # noqa: F401
from albert.config import get_settings
from albert.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


POSTGRES_ONLY_INDEXES = {
    "ix_memory_chunks_embedding_hnsw",
    "ix_memories_search_vector",
}
POSTGRES_ONLY_COLUMNS = {("memories", "search_vector")}


def include_object(object_, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # The HNSW index and the generated tsvector column exist only on PostgreSQL
    # and are created with raw DDL in the migrations, so autogenerate must not
    # try to drop them.
    if type_ == "index" and reflected and name in POSTGRES_ONLY_INDEXES:
        return False
    if type_ == "column" and reflected and (object_.table.name, name) in POSTGRES_ONLY_COLUMNS:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

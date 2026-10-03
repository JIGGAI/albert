from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import JSON, Engine, create_engine, event, text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from albert.config import get_settings


class Base(DeclarativeBase):
    pass


class VectorOrJSON(TypeDecorator):
    """pgvector in production and JSON in SQLite-based unit tests."""

    impl = JSON
    cache_ok = True

    def __init__(self, dimensions: int, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.dimensions = dimensions

    def load_dialect_impl(self, dialect: Dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import VECTOR

            return dialect.type_descriptor(VECTOR(self.dimensions))
        return dialect.type_descriptor(JSON())


def _make_engine() -> Engine:
    settings = get_settings()
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(settings.database_url, **kwargs)
    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_session() -> Generator[Session, None, None]:
    """Sync session dependency: FastAPI runs sync dependencies and handlers in
    its threadpool, so blocking database and provider I/O never stalls the loop."""
    with SessionLocal() as session:
        yield session


def initialize_database() -> None:
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)

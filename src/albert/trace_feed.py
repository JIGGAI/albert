"""Cursor-based trace feed.

Rows always come from the traces table, ordered by (written_at, id): rows land
in completion order, so a request that started early but finished late is still
delivered, and a client that reconnects with its last event id misses nothing.
On PostgreSQL a LISTEN on a dedicated connection (never the SQLAlchemy pool)
only wakes the poll early; elsewhere the feed sleeps between polls.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import anyio
from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from albert.models import Trace
from albert.trace_writer import NOTIFY_CHANNEL

logger = logging.getLogger("albert.trace_feed")
Cursor = tuple[datetime, UUID]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def event_id(trace: Trace) -> str:
    return f"{_utc(trace.written_at).astimezone(UTC).isoformat()}|{trace.id}"


def parse_event_id(value: str | None) -> Cursor | None:
    if not value:
        return None
    try:
        raw_time, raw_id = value.split("|", 1)
        return datetime.fromisoformat(raw_time), UUID(raw_id)
    except ValueError:
        return None


def trace_summary(trace: Trace) -> dict[str, Any]:
    def opt(value: UUID | None) -> str | None:
        return str(value) if value is not None else None

    return {
        "id": str(trace.id),
        "kind": trace.kind,
        "name": trace.name,
        "organization_id": opt(trace.organization_id),
        "principal_id": opt(trace.principal_id),
        "status": trace.status,
        "http_status": trace.http_status,
        "started_at": _utc(trace.started_at).astimezone(UTC).isoformat(),
        "duration_ms": trace.duration_ms,
        "summary": trace.summary,
    }


def psycopg_dsn(database_url: str) -> str:
    """SQLAlchemy URL (postgresql+psycopg://…) to a plain libpq URL."""
    scheme, _, rest = database_url.partition("://")
    return f"{scheme.split('+', 1)[0]}://{rest}"


class TraceFeed:
    def __init__(
        self, session_factory: Callable[[], Session], engine: Engine, database_url: str
    ) -> None:
        self._sessions = session_factory
        self._postgres = engine.dialect.name == "postgresql"
        self._dsn = psycopg_dsn(database_url)

    def _normalize(self, when: datetime) -> datetime:
        if self._postgres:
            return _utc(when)
        # SQLite stores naive UTC strings; compare like with like.
        return when.astimezone(UTC).replace(tzinfo=None) if when.tzinfo else when

    def _newer_than(self, after: Cursor | None, limit: int = 200) -> list[Trace]:
        with self._sessions() as session:
            statement = select(Trace)
            if after is not None:
                cursor_time, cursor_id = self._normalize(after[0]), after[1]
                statement = statement.where(
                    or_(
                        Trace.written_at > cursor_time,
                        (Trace.written_at == cursor_time) & (Trace.id > cursor_id),
                    )
                )
            return session.scalars(
                statement.order_by(Trace.written_at, Trace.id).limit(limit)
            ).all()

    def _latest_cursor(self) -> Cursor | None:
        with self._sessions() as session:
            latest = session.scalar(
                select(Trace).order_by(Trace.written_at.desc(), Trace.id.desc()).limit(1)
            )
        return (latest.written_at, latest.id) if latest is not None else None

    async def _open_listener(self):  # type: ignore[no-untyped-def]
        """A connection of our own: the pool must never see LISTEN or autocommit."""
        if not self._postgres:
            return None
        try:
            import psycopg

            connection = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            await connection.execute(f"LISTEN {NOTIFY_CHANNEL}")
            return connection
        except Exception:
            logger.warning("could not LISTEN on %s; polling instead", NOTIFY_CHANNEL, exc_info=True)
            return None

    async def _wait(self, listener, poll_seconds: float) -> None:  # type: ignore[no-untyped-def]
        if listener is None:
            await anyio.sleep(poll_seconds)
            return
        try:
            async for _notification in listener.notifies(timeout=poll_seconds, stop_after=1):
                break
        except Exception:
            logger.warning("LISTEN wait failed; falling back to sleep", exc_info=True)
            await anyio.sleep(poll_seconds)

    def events(
        self, after: Cursor | None, poll_seconds: float = 1.0
    ) -> AsyncIterator[dict[str, Any] | None]:
        """Yield trace events newer than the cursor; yield None on each idle wait.

        The starting cursor is resolved here, on subscription, not when the
        generator is first iterated, so a trace written in between is delivered
        rather than treated as history. A None lets the HTTP layer send a
        keepalive without waiting for the next trace. Database reads run in a
        worker thread; waits do not hold one.
        """
        cursor = after if after is not None else self._latest_cursor()
        return self._iterate(cursor, poll_seconds)

    async def _iterate(
        self, cursor: Cursor | None, poll_seconds: float
    ) -> AsyncIterator[dict[str, Any] | None]:
        listener = await self._open_listener()
        try:
            while True:
                rows = await anyio.to_thread.run_sync(self._newer_than, cursor)
                for trace in rows:
                    cursor = (trace.written_at, trace.id)
                    yield {"id": event_id(trace), "data": trace_summary(trace)}
                if not rows:
                    await self._wait(listener, poll_seconds)
                    yield None
        finally:
            if listener is not None:
                try:
                    await listener.close()
                except Exception:  # pragma: no cover
                    pass

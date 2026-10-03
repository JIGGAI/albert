"""Cursor-based trace feed.

Rows always come from the traces table, ordered by (started_at, id), so a
client that reconnects with its last event id misses nothing. On PostgreSQL a
LISTEN on the writer's channel only wakes the poll early; on other databases
the feed sleeps between polls.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from albert.models import Trace
from albert.trace_writer import NOTIFY_CHANNEL

logger = logging.getLogger("albert.trace_feed")
Cursor = tuple[datetime, UUID]


def event_id(trace: Trace) -> str:
    return f"{trace.started_at.isoformat()}|{trace.id}"


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

    started = trace.started_at if trace.started_at.tzinfo else trace.started_at.replace(tzinfo=UTC)

    return {
        "id": str(trace.id),
        "kind": trace.kind,
        "name": trace.name,
        "organization_id": opt(trace.organization_id),
        "principal_id": opt(trace.principal_id),
        "status": trace.status,
        "http_status": trace.http_status,
        "started_at": started.astimezone(UTC).isoformat(),
        "duration_ms": trace.duration_ms,
        "summary": trace.summary,
    }


class TraceFeed:
    def __init__(self, session_factory: Callable[[], Session], engine: Engine) -> None:
        self._sessions = session_factory
        self._engine = engine
        self._postgres = engine.dialect.name == "postgresql"

    def _normalize(self, when: datetime) -> datetime:
        if self._postgres:
            return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
        # SQLite stores naive UTC strings; compare like with like.
        return when.astimezone(UTC).replace(tzinfo=None) if when.tzinfo else when

    def _newer_than(self, after: Cursor | None, limit: int = 200) -> list[Trace]:
        with self._sessions() as session:
            statement = select(Trace)
            if after is not None:
                cursor_time, cursor_id = self._normalize(after[0]), after[1]
                statement = statement.where(
                    or_(
                        Trace.started_at > cursor_time,
                        (Trace.started_at == cursor_time) & (Trace.id > cursor_id),
                    )
                )
            return session.scalars(
                statement.order_by(Trace.started_at, Trace.id).limit(limit)
            ).all()

    def _latest_cursor(self) -> Cursor | None:
        with self._sessions() as session:
            latest = session.scalar(
                select(Trace).order_by(Trace.started_at.desc(), Trace.id.desc()).limit(1)
            )
        return (latest.started_at, latest.id) if latest is not None else None

    def _listener(self):  # type: ignore[no-untyped-def]
        if not self._postgres:
            return None
        try:
            connection = self._engine.raw_connection().driver_connection
            connection.autocommit = True
            connection.execute(f"LISTEN {NOTIFY_CHANNEL}")
            return connection
        except Exception:
            logger.warning("could not LISTEN on %s; polling instead", NOTIFY_CHANNEL, exc_info=True)
            return None

    def _wait(self, stop: threading.Event, poll_seconds: float, listener) -> None:  # type: ignore[no-untyped-def]
        if listener is None:
            stop.wait(poll_seconds)
            return
        try:
            for _notification in listener.notifies(timeout=poll_seconds, stop_after=1):
                break
        except Exception:
            logger.warning("LISTEN wait failed; falling back to sleep", exc_info=True)
            stop.wait(poll_seconds)

    def events(
        self, after: Cursor | None, stop: threading.Event, poll_seconds: float = 1.0
    ) -> Iterator[dict[str, Any] | None]:
        """Yield trace events newer than the cursor; yield None on each idle wait.

        The starting cursor is resolved now, not on first iteration, so a trace
        written between subscribing and consuming is delivered rather than
        silently treated as history. A None lets the HTTP layer send a keepalive
        without waiting for the next trace.
        """
        cursor = after if after is not None else self._latest_cursor()
        return self._iterate(cursor, stop, poll_seconds)

    def _iterate(
        self, cursor: Cursor | None, stop: threading.Event, poll_seconds: float
    ) -> Iterator[dict[str, Any] | None]:
        listener = self._listener()
        try:
            while not stop.is_set():
                rows = self._newer_than(cursor)
                for trace in rows:
                    cursor = (trace.started_at, trace.id)
                    yield {"id": event_id(trace), "data": trace_summary(trace)}
                if not rows:
                    self._wait(stop, poll_seconds, listener)
                    yield None
        finally:
            if listener is not None:
                try:
                    listener.close()
                except Exception:  # pragma: no cover
                    pass

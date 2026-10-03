from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from albert.db import SessionLocal, engine
from albert.models import Trace
from albert.trace_feed import TraceFeed

pytestmark = pytest.mark.anyio


def _insert(name: str, started_at: datetime | None = None) -> Trace:
    with SessionLocal() as session:
        trace = Trace(
            kind="request",
            name=name,
            status="ok",
            started_at=started_at or datetime.now(UTC),
            duration_ms=1,
            summary={},
            spans=[],
        )
        session.add(trace)
        session.commit()
        return trace


async def _next_event(events):  # type: ignore[no-untyped-def]
    async for event in events:
        if event is not None:
            return event
    raise AssertionError("feed ended without an event")


async def test_feed_delivers_traces_inserted_after_subscription() -> None:
    feed = TraceFeed(SessionLocal, engine, "sqlite://")
    before = _insert("before-subscribe")
    events = feed.events(after=(before.written_at, before.id), poll_seconds=0.05)
    inserted = _insert("after-subscribe")
    event = await _next_event(events)
    await events.aclose()
    assert event["id"].endswith(str(inserted.id))
    assert event["data"]["name"] == "after-subscribe"


async def test_feed_backfills_from_last_event_id() -> None:
    feed = TraceFeed(SessionLocal, engine, "sqlite://")
    first = _insert("backfill-1")
    second = _insert("backfill-2")
    events = feed.events(after=(first.written_at, first.id), poll_seconds=0.05)
    event = await _next_event(events)
    await events.aclose()
    assert event["data"]["name"] == "backfill-2"
    assert event["id"].endswith(str(second.id))


async def test_feed_without_cursor_starts_at_now() -> None:
    feed = TraceFeed(SessionLocal, engine, "sqlite://")
    _insert("historic")
    events = feed.events(after=None, poll_seconds=0.05)
    fresh = _insert("live")
    event = await _next_event(events)
    await events.aclose()
    assert event["data"]["name"] == "live"
    assert event["id"].endswith(str(fresh.id))


async def test_feed_delivers_a_slow_trace_that_started_before_a_delivered_one() -> None:
    """Rows land in completion order; a request that started earlier but finished
    later must still reach the feed (worker jobs and slow searches depend on it)."""
    feed = TraceFeed(SessionLocal, engine, "sqlite://")
    _insert("fast", started_at=datetime.now(UTC))
    events = feed.events(after=None, poll_seconds=0.05)
    slow = _insert("slow", started_at=datetime.now(UTC) - timedelta(seconds=30))
    event = await _next_event(events)
    await events.aclose()
    assert event["data"]["name"] == "slow"
    assert event["id"].endswith(str(slow.id))


async def test_postgres_listener_uses_a_dedicated_connection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The LISTEN connection must never come from (or return to) the SQLAlchemy pool."""
    import psycopg

    from albert import trace_feed

    opened: list[dict] = []
    executed: list[str] = []

    class FakeListener:
        async def execute(self, sql: str) -> None:
            executed.append(sql)

        async def notifies(self, timeout: float, stop_after: int):  # type: ignore[no-untyped-def]
            yield SimpleNamespace(payload="x")

        async def close(self) -> None:
            executed.append("close")

    async def fake_connect(url: str, **kwargs):  # type: ignore[no-untyped-def]
        opened.append({"url": url, **kwargs})
        return FakeListener()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", staticmethod(fake_connect))
    monkeypatch.setattr(trace_feed.TraceFeed, "_newer_than", lambda self, after: [])
    monkeypatch.setattr(trace_feed.TraceFeed, "_latest_cursor", lambda self: None)
    feed = TraceFeed(SessionLocal, engine, "postgresql+psycopg://u:p@db:5432/albert")
    feed._postgres = True
    events = feed.events(after=None, poll_seconds=0.01)
    assert await events.__anext__() is None
    await events.aclose()
    assert opened == [{"url": "postgresql://u:p@db:5432/albert", "autocommit": True}]
    assert executed[0] == "LISTEN albert_traces"
    assert executed[-1] == "close"

from __future__ import annotations

import threading
from datetime import UTC, datetime

from albert.db import SessionLocal, engine
from albert.models import Trace
from albert.trace_feed import TraceFeed


def _insert(name: str) -> Trace:
    with SessionLocal() as session:
        trace = Trace(
            kind="request",
            name=name,
            status="ok",
            started_at=datetime.now(UTC),
            duration_ms=1,
            summary={},
            spans=[],
        )
        session.add(trace)
        session.commit()
        return trace


def _next_event(events):  # type: ignore[no-untyped-def]
    for event in events:
        if event is not None:
            return event
    raise AssertionError("feed ended without an event")


def test_feed_delivers_traces_inserted_after_subscription() -> None:
    feed = TraceFeed(SessionLocal, engine)
    stop = threading.Event()
    before = _insert("before-subscribe")
    events = feed.events(after=(before.started_at, before.id), stop=stop, poll_seconds=0.05)
    inserted = _insert("after-subscribe")
    event = _next_event(events)
    stop.set()
    assert event["id"].endswith(str(inserted.id))
    assert event["data"]["name"] == "after-subscribe"


def test_feed_backfills_from_last_event_id() -> None:
    feed = TraceFeed(SessionLocal, engine)
    first = _insert("backfill-1")
    second = _insert("backfill-2")
    stop = threading.Event()
    events = feed.events(after=(first.started_at, first.id), stop=stop, poll_seconds=0.05)
    event = _next_event(events)
    stop.set()
    assert event["data"]["name"] == "backfill-2"
    assert event["id"].endswith(str(second.id))


def test_feed_without_cursor_starts_at_now() -> None:
    feed = TraceFeed(SessionLocal, engine)
    _insert("historic")
    stop = threading.Event()
    events = feed.events(after=None, stop=stop, poll_seconds=0.05)
    fresh = _insert("live")
    event = _next_event(events)
    stop.set()
    assert event["data"]["name"] == "live"
    assert event["id"].endswith(str(fresh.id))

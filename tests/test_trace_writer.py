from __future__ import annotations

import random
import time

from sqlalchemy import func, select

from albert.db import SessionLocal
from albert.models import Trace
from albert.recorder import Recorder
from albert.trace_writer import TraceWriter


def _row(status: str = "ok") -> dict:
    return Recorder("request", "GET /v1/health/live").finish(status=status)


def _count() -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count(Trace.id))) or 0


def test_submitted_rows_are_written_in_batches() -> None:
    writer = TraceWriter(SessionLocal, queue_size=100, sample_rate=1.0)
    before = _count()
    for _ in range(7):
        assert writer.submit(_row())
    writer.flush()
    assert _count() == before + 7
    assert writer.written == 7


def test_overflow_drops_and_counts() -> None:
    writer = TraceWriter(SessionLocal, queue_size=2, sample_rate=1.0)
    assert writer.submit(_row())
    assert writer.submit(_row())
    assert writer.submit(_row()) is False
    assert writer.dropped == 1
    writer.flush()


def test_sampling_keeps_degraded_and_error_traces(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(random, "random", lambda: 0.99)
    writer = TraceWriter(SessionLocal, queue_size=10, sample_rate=0.0)
    assert writer.submit(_row("ok")) is False
    assert writer.submit(_row("degraded"))
    assert writer.submit(_row("error"))
    writer.flush()
    assert writer.written == 2


def test_background_thread_drains_without_explicit_flush() -> None:
    writer = TraceWriter(SessionLocal, queue_size=10, sample_rate=1.0, flush_interval=0.05)
    writer.start()
    try:
        writer.submit(_row())
        deadline = time.monotonic() + 2
        while writer.written < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.written == 1
    finally:
        writer.stop()

from __future__ import annotations

import random
import threading
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


def test_flush_waits_for_an_in_flight_background_write(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    writer = TraceWriter(SessionLocal, queue_size=10, sample_rate=1.0)
    before = _count()
    write_started = threading.Event()
    release_write = threading.Event()
    foreground_started = threading.Event()
    foreground_done = threading.Event()
    original_write = writer._write

    def blocked_write(batch: list[dict]) -> None:
        write_started.set()
        assert release_write.wait(timeout=2)
        original_write(batch)

    def foreground_flush() -> None:
        foreground_started.set()
        writer.flush()
        foreground_done.set()

    monkeypatch.setattr(writer, "_write", blocked_write)
    assert writer.submit(_row())
    background = threading.Thread(target=writer.flush)
    foreground = threading.Thread(target=foreground_flush)
    background.start()
    assert write_started.wait(timeout=2)
    foreground.start()
    try:
        assert foreground_started.wait(timeout=2)
        assert not foreground_done.wait(timeout=0.05)
    finally:
        release_write.set()
        background.join(timeout=2)
        foreground.join(timeout=2)
    assert foreground_done.is_set()
    assert _count() == before + 1

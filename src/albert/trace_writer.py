"""Off-thread persistence for recorder rows.

The request thread only enqueues. One background thread drains the queue in
batches on its own session and, on PostgreSQL, notifies listeners so live
feeds wake without polling.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.models import Trace

logger = logging.getLogger("albert.trace_writer")
NOTIFY_CHANNEL = "albert_traces"
ALWAYS_KEEP = {"error", "degraded"}


class TraceWriter:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        queue_size: int,
        sample_rate: float,
        flush_interval: float = 0.25,
    ) -> None:
        self._sessions = session_factory
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self._sample_rate = sample_rate
        self._interval = flush_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.dropped = 0
        self.written = 0

    def submit(self, row: dict[str, Any]) -> bool:
        if row["status"] not in ALWAYS_KEEP and random.random() >= self._sample_rate:
            return False
        try:
            self._queue.put_nowait(row)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="albert-trace-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.flush()

    def flush(self) -> None:
        batch: list[dict[str, Any]] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._write(batch)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            try:
                self.flush()
            except Exception:  # pragma: no cover - never let the thread die
                logger.exception("trace flush failed")

    def _write(self, batch: list[dict[str, Any]]) -> None:
        with self._lock:
            try:
                with self._sessions() as session:
                    session.add_all(Trace(**row) for row in batch)
                    session.flush()
                    if session.bind is not None and session.bind.dialect.name == "postgresql":
                        for row in batch:
                            session.execute(
                                text("SELECT pg_notify(:channel, :payload)"),
                                {"channel": NOTIFY_CHANNEL, "payload": str(row["id"])},
                            )
                    session.commit()
                self.written += len(batch)
            except Exception:
                self.dropped += len(batch)
                logger.exception("dropping %d traces after write failure", len(batch))


@lru_cache
def get_trace_writer() -> TraceWriter:
    from albert.db import SessionLocal

    settings = get_settings()
    writer = TraceWriter(
        SessionLocal,
        queue_size=settings.trace_queue_size,
        sample_rate=settings.trace_sample_rate,
    )
    writer.start()
    return writer

# Memory Console (slice one) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every Albert request and worker job as a trace, stream traces live, and render the memory graph in 3D with any trace's retrieval path highlighted.

**Architecture:** A context-variable `Recorder` collects spans inside the existing sync handlers and worker; a background `TraceWriter` thread inserts one JSON row per trace and `pg_notify`s. New `/v1/console/*` endpoints (capability `console.read`) serve traces, an SSE feed, a capped graph snapshot and an overview. A Next.js app in `console/` proxies to those endpoints with a server-held key and renders Live, Replay and Explorer screens (three.js via `react-force-graph-3d`).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, psycopg 3 (LISTEN/NOTIFY), Next.js 15 (app router, TypeScript), React 19, `react-force-graph-3d`, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-03-memory-console-design.md`

## Global Constraints

- Traces store ids, numbers and the search query text only; **never** memory `subject` or `content`.
- Span `detail` is capped at 8 KB serialized; retrieval candidate lists at 50 entries.
- Recording never blocks or fails a request: enqueue only on the request thread; drop on overflow.
- Recording overhead on `/v1/search` (SQLite stack): median under 5 % and under 2 ms absolute, enforced in CI.
- All new REST handlers are sync `def` (existing test `test_endpoints_do_not_run_blocking_io_on_the_event_loop` enforces this).
- Console endpoints require capability `console.read`; `admin` implies it.
- Console container binds `127.0.0.1:8082` by default; the operator key lives only in the Next.js server environment.
- Settings use the `ALBERT_` prefix: `ALBERT_TRACE_SAMPLE_RATE` (1.0), `ALBERT_TRACE_RETENTION_DAYS` (14), `ALBERT_TRACE_QUEUE_SIZE` (1000), `ALBERT_CONSOLE_GRAPH_CACHE_SECONDS` (30).
- `ruff check .` clean; line length 100.

## Review Focus

1. A request that raises before `authenticate` completes (malformed bearer) must still produce an `error` trace with `organization_id = NULL` — Task 4 test `test_unauthenticated_request_is_traced`.
2. A search whose vector backend degrades must produce a `degraded` trace and still be kept at sample rate 0 — Task 3 test `test_sampling_keeps_degraded_and_error_traces`.
3. A span `detail` larger than 8 KB (a backend returning hundreds of candidates) must be capped, not rejected — Task 1 test `test_detail_is_capped`.
4. The SSE feed must deliver a trace inserted after the client subscribed and backfill after reconnect with `Last-Event-ID` — Task 7 tests.
5. The graph endpoint with a `limit` above 5,000 must answer 422, and a capped snapshot must say `truncated: true` — Task 6 test `test_graph_snapshot_is_capped`.

---

### Task 1: Recorder core

**Files:**
- Create: `src/albert/recorder.py`
- Test: `tests/test_recorder.py`

**Interfaces:**
- Produces:
  - `class Recorder(kind: str, name: str)` with `.spans: list[dict]`, `.organization_id`, `.principal_id`, `.status`, `.http_status`, `.summary: dict`, `.finish(status: str | None = None) -> dict` (returns the row dict for `traces`), `.set_identity(organization_id, principal_id)`, `.note(**summary_fields)` (merges into summary; `query` capped at 500 chars).
  - `current_recorder() -> Recorder | None`
  - `@contextmanager activate(recorder: Recorder)` sets/resets the ContextVar.
  - `@contextmanager span(name: str, **detail) -> SpanHandle` where `SpanHandle.detail: dict` can be mutated inside the block and `SpanHandle.set(**fields)` merges. No-op (yields a handle whose data is discarded) when no recorder is active.
  - `SPAN_DETAIL_LIMIT = 8_192`, `CANDIDATE_LIMIT = 50`, `cap_candidates(items: list[dict]) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_recorder.py
from __future__ import annotations

import json
import time

import pytest

from albert import recorder as rec


def test_spans_record_order_offsets_and_durations() -> None:
    r = rec.Recorder("request", "POST /v1/search")
    with rec.activate(r):
        with rec.span("auth"):
            time.sleep(0.005)
        with rec.span("lexical", candidates_total=3) as handle:
            handle.set(candidates=[{"id": "a", "score": 1.0, "rank": 1}])
    row = r.finish()
    names = [s["name"] for s in row["spans"]]
    assert names == ["auth", "lexical"]
    assert [s["seq"] for s in row["spans"]] == [0, 1]
    assert row["spans"][0]["duration_ms"] >= 4
    assert row["spans"][1]["started_offset_ms"] >= row["spans"][0]["duration_ms"]
    assert row["spans"][1]["detail"]["candidates_total"] == 3
    assert row["spans"][1]["detail"]["candidates"][0]["id"] == "a"
    assert row["status"] == "ok"
    assert row["duration_ms"] >= 5


def test_exception_marks_span_error_and_reraises() -> None:
    r = rec.Recorder("job", "enrich_memory")
    with rec.activate(r), pytest.raises(ValueError):
        with rec.span("embed"):
            raise ValueError("boom")
    row = r.finish()
    assert row["spans"][0]["status"] == "error"
    assert row["spans"][0]["detail"]["error"] == "ValueError: boom"
    assert row["status"] == "error"


def test_span_is_noop_without_active_recorder() -> None:
    assert rec.current_recorder() is None
    with rec.span("vector") as handle:
        handle.set(candidates=[])
    assert rec.current_recorder() is None


def test_detail_is_capped() -> None:
    r = rec.Recorder("request", "POST /v1/search")
    with rec.activate(r):
        with rec.span("vector") as handle:
            handle.set(candidates=[{"id": f"{i:032x}", "score": 0.5, "rank": i} for i in range(400)])
            handle.set(blob="x" * 20_000)
    row = r.finish()
    detail = row["spans"][0]["detail"]
    assert len(detail["candidates"]) == rec.CANDIDATE_LIMIT
    assert len(json.dumps(detail)) <= rec.SPAN_DETAIL_LIMIT
    assert detail["truncated"] is True


def test_summary_query_is_capped_and_identity_recorded() -> None:
    r = rec.Recorder("request", "POST /v1/search")
    r.note(query="q" * 2000, hits=3)
    r.set_identity(organization_id="org", principal_id="p")
    row = r.finish(status="degraded")
    assert len(row["summary"]["query"]) == 500
    assert row["summary"]["hits"] == 3
    assert row["organization_id"] == "org"
    assert row["status"] == "degraded"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_recorder.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'albert.recorder'`

- [ ] **Step 3: Write the implementation**

```python
# src/albert/recorder.py
"""In-process flight recorder: per-request / per-job spans with bounded detail.

Spans never hold memory content. Callers pass ids, scores, counts and flags;
the console fetches anything else through the authorized API.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

SPAN_DETAIL_LIMIT = 8_192
CANDIDATE_LIMIT = 50
SUMMARY_QUERY_LIMIT = 500

_current: ContextVar[Recorder | None] = ContextVar("albert_recorder", default=None)


def current_recorder() -> Recorder | None:
    return _current.get()


def cap_candidates(items: list[dict]) -> list[dict]:
    return items[:CANDIDATE_LIMIT]


def _cap_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Shrink a detail dict until it serializes under SPAN_DETAIL_LIMIT."""
    capped = dict(detail)
    truncated = False
    for key, value in list(capped.items()):
        if isinstance(value, list) and len(value) > CANDIDATE_LIMIT:
            capped[key] = value[:CANDIDATE_LIMIT]
            truncated = True
    while len(json.dumps(capped, default=str)) > SPAN_DETAIL_LIMIT:
        truncated = True
        largest = max(capped, key=lambda k: len(json.dumps(capped[k], default=str)))
        value = capped[largest]
        if isinstance(value, list) and value:
            capped[largest] = value[: max(1, len(value) // 2)]
        elif isinstance(value, str) and len(value) > 64:
            capped[largest] = value[: max(16, len(value) // 2)]
        else:
            del capped[largest]
        if not capped:
            break
    if truncated:
        capped["truncated"] = True
    return capped


class SpanHandle:
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail

    def set(self, **fields: Any) -> None:
        self.detail.update(fields)


class Recorder:
    def __init__(self, kind: str, name: str) -> None:
        self.id = uuid.uuid4()
        self.kind = kind
        self.name = name
        self.started_at = datetime.now(UTC)
        self._start = time.perf_counter()
        self.spans: list[dict[str, Any]] = []
        self.organization_id: Any = None
        self.principal_id: Any = None
        self.http_status: int | None = None
        self.status: str | None = None
        self.summary: dict[str, Any] = {}

    def set_identity(self, organization_id: Any, principal_id: Any) -> None:
        self.organization_id = organization_id
        self.principal_id = principal_id

    def note(self, **fields: Any) -> None:
        if "query" in fields and isinstance(fields["query"], str):
            fields["query"] = fields["query"][:SUMMARY_QUERY_LIMIT]
        self.summary.update(fields)

    def add_span(self, name: str, started: float, detail: dict[str, Any], status: str) -> None:
        now = time.perf_counter()
        self.spans.append(
            {
                "name": name,
                "seq": len(self.spans),
                "started_offset_ms": round((started - self._start) * 1000, 3),
                "duration_ms": round((now - started) * 1000, 3),
                "status": status,
                "detail": _cap_detail(detail),
            }
        )

    def finish(self, status: str | None = None) -> dict[str, Any]:
        if status is None:
            status = self.status or (
                "error" if any(s["status"] == "error" for s in self.spans) else "ok"
            )
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "organization_id": self.organization_id,
            "principal_id": self.principal_id,
            "status": status,
            "http_status": self.http_status,
            "started_at": self.started_at,
            "duration_ms": round((time.perf_counter() - self._start) * 1000, 3),
            "summary": dict(self.summary),
            "spans": list(self.spans),
        }


@contextmanager
def activate(recorder: Recorder) -> Iterator[Recorder]:
    token = _current.set(recorder)
    try:
        yield recorder
    finally:
        _current.reset(token)


@contextmanager
def span(name: str, **detail: Any) -> Iterator[SpanHandle]:
    recorder = _current.get()
    handle = SpanHandle(dict(detail))
    if recorder is None:
        yield handle
        return
    started = time.perf_counter()
    try:
        yield handle
    except Exception as exc:
        handle.set(error=f"{type(exc).__name__}: {exc}"[:500])
        recorder.add_span(name, started, handle.detail, "error")
        raise
    recorder.add_span(name, started, handle.detail, "ok")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_recorder.py -q && .venv/bin/ruff check src/albert/recorder.py tests/test_recorder.py`
Expected: 5 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/albert/recorder.py tests/test_recorder.py
git commit -m "feat(recorder): context-variable span recorder with bounded detail"
```

---

### Task 2: Trace model, settings and migration 0004

**Files:**
- Modify: `src/albert/models.py` (append `Trace` model)
- Modify: `src/albert/config.py` (trace settings)
- Create: `migrations/versions/0004_traces.py`
- Modify: `.env.example`
- Test: `tests/test_traces_model.py`

**Interfaces:**
- Produces: `class Trace(Base)` table `traces` with columns `id, kind, name, organization_id, principal_id, status, http_status, started_at, duration_ms, summary(JSON), spans(JSON)`; index `ix_traces_started (started_at desc, id)` and `ix_traces_org_started (organization_id, started_at)`.
- Settings: `trace_sample_rate: float = 1.0`, `trace_retention_days: int = 14`, `trace_queue_size: int = 1000`, `console_graph_cache_seconds: int = 30`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_traces_model.py
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from albert.config import get_settings
from albert.db import SessionLocal
from albert.models import Trace


def test_trace_row_round_trips_json_columns() -> None:
    row = Trace(
        kind="request",
        name="POST /v1/search",
        organization_id=uuid4(),
        principal_id=None,
        status="ok",
        http_status=200,
        started_at=datetime.now(UTC),
        duration_ms=12.5,
        summary={"query": "hello", "hits": 2},
        spans=[{"name": "auth", "seq": 0, "detail": {}}],
    )
    with SessionLocal() as session:
        session.add(row)
        session.commit()
        loaded = session.scalar(select(Trace).where(Trace.id == row.id))
        assert loaded is not None
        assert loaded.summary["hits"] == 2
        assert loaded.spans[0]["name"] == "auth"


def test_trace_settings_have_documented_defaults() -> None:
    settings = get_settings()
    assert settings.trace_sample_rate == 1.0
    assert settings.trace_retention_days == 14
    assert settings.trace_queue_size == 1000
    assert settings.console_graph_cache_seconds == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_traces_model.py -q`
Expected: FAIL with `ImportError: cannot import name 'Trace'`

- [ ] **Step 3: Add the model, settings, migration and env example**

Append to `src/albert/models.py`:

```python
class Trace(Base):
    """One recorded request or worker job; spans are embedded JSON.

    Holds ids, scores and timings only. Memory content never enters this table.
    """

    __tablename__ = "traces"
    __table_args__ = (
        Index("ix_traces_started", "started_at", "id"),
        Index("ix_traces_org_started", "organization_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column()
    principal_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
```

Add to `Settings` in `src/albert/config.py` after `max_chunks_per_memory`:

```python
    trace_sample_rate: float = Field(default=1.0, ge=0, le=1)
    trace_retention_days: int = Field(default=14, ge=1, le=365)
    trace_queue_size: int = Field(default=1000, ge=10, le=100_000)
    console_graph_cache_seconds: int = Field(default=30, ge=0, le=3600)
```

Create `migrations/versions/0004_traces.py`:

```python
"""Flight-recorder traces.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("spans", sa.JSON(), nullable=False),
    )
    for name, columns in (
        ("ix_traces_kind", ["kind"]),
        ("ix_traces_name", ["name"]),
        ("ix_traces_status", ["status"]),
        ("ix_traces_started", ["started_at", "id"]),
        ("ix_traces_org_started", ["organization_id", "started_at"]),
    ):
        op.create_index(name, "traces", columns)


def downgrade() -> None:
    op.drop_table("traces")
```

Append to `.env.example`:

```dotenv
# Flight recorder: fraction of ok traces kept (errors/degraded always kept) and retention.
ALBERT_TRACE_SAMPLE_RATE=1.0
ALBERT_TRACE_RETENTION_DAYS=14
# Operator key for the console service; issue with
#   albert-admin create-key --principal-id <console principal> --capabilities console.read
ALBERT_CONSOLE_API_KEY=
ALBERT_CONSOLE_BIND=127.0.0.1
```

- [ ] **Step 4: Run tests and the migration round trip**

Run: `.venv/bin/pytest tests/test_traces_model.py -q`
Expected: 2 passed.

Run:
```bash
rm -f /tmp/albert-mig.sqlite3
ALBERT_DATABASE_URL=sqlite:////tmp/albert-mig.sqlite3 ALBERT_API_KEY_PEPPER=test-pepper-that-is-longer-than-thirty-two-characters .venv/bin/alembic upgrade head
ALBERT_DATABASE_URL=sqlite:////tmp/albert-mig.sqlite3 ALBERT_API_KEY_PEPPER=test-pepper-that-is-longer-than-thirty-two-characters .venv/bin/alembic downgrade 0003
ALBERT_DATABASE_URL=sqlite:////tmp/albert-mig.sqlite3 ALBERT_API_KEY_PEPPER=test-pepper-that-is-longer-than-thirty-two-characters .venv/bin/alembic upgrade head
```
Expected: `Running upgrade 0003 -> 0004` appears; no errors.

- [ ] **Step 5: Commit**

```bash
git add src/albert/models.py src/albert/config.py migrations/versions/0004_traces.py .env.example tests/test_traces_model.py
git commit -m "feat(recorder): traces table, settings and migration 0004"
```

---

### Task 3: TraceWriter (queue, batching, sampling, notify)

**Files:**
- Create: `src/albert/trace_writer.py`
- Test: `tests/test_trace_writer.py`

**Interfaces:**
- Consumes: `Trace` model (Task 2), `Recorder.finish()` row dicts (Task 1), `SessionLocal`.
- Produces: `class TraceWriter(session_factory, *, queue_size: int, sample_rate: float, flush_interval: float = 0.25)` with `.submit(row: dict) -> bool` (False when dropped), `.start()`, `.stop(timeout=5.0)` (flushes), `.flush()` (synchronous drain, used by tests), `.dropped: int`, `.written: int`. Module-level `get_trace_writer() -> TraceWriter` (lru_cache, built from settings, started on first use). `NOTIFY_CHANNEL = "albert_traces"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trace_writer.py
from __future__ import annotations

import random

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
        import time

        deadline = time.monotonic() + 2
        while writer.written < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.written == 1
    finally:
        writer.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_trace_writer.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'albert.trace_writer'`

- [ ] **Step 3: Write the implementation**

```python
# src/albert/trace_writer.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_trace_writer.py -q && .venv/bin/ruff check .`
Expected: 4 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/albert/trace_writer.py tests/test_trace_writer.py
git commit -m "feat(recorder): queued trace writer with sampling and pg_notify"
```

---

### Task 4: Request middleware, worker wrapping, instrumentation points

**Files:**
- Create: `src/albert/tracing.py` (ASGI middleware + worker helper)
- Modify: `src/albert/api.py` (install middleware; `note()` in search/context/graph handlers)
- Modify: `src/albert/security.py:authenticate` (`auth` span, `set_identity`)
- Modify: `src/albert/services.py` (`resolve_scope`, `chunk`, `embed`, `classify`, `write_edges` spans)
- Modify: `src/albert/search.py` (`lexical`, `vector`, `graph`, `fuse` spans with candidates)
- Modify: `src/albert/worker.py:process_job` (job recorder)
- Test: `tests/test_tracing.py`

**Interfaces:**
- Consumes: `Recorder`, `activate`, `span`, `current_recorder` (Task 1); `get_trace_writer()` (Task 3).
- Produces: `class RecordingMiddleware(app)` (pure ASGI, installed via `app.add_middleware`), `@contextmanager traced_job(job: Job)` used by the worker. Span names exactly: requests `auth, resolve_scope, lexical, vector, graph, fuse`; jobs `classify, chunk, embed, write_edges`. Candidate dict shape `{"id": str, "kind": "memory"|"relationship", "score": float, "rank": int}`; `fuse` hit shape `{"id": str, "rank": int, "rrf": {backend: float}, "backends": [str]}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tracing.py
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from albert.api import app
from albert.db import SessionLocal
from albert.models import Trace
from albert.trace_writer import get_trace_writer

from .conftest import drain_jobs

pytestmark = pytest.mark.anyio


def _latest(name: str) -> Trace:
    get_trace_writer().flush()
    with SessionLocal() as session:
        trace = session.scalar(
            select(Trace).where(Trace.name == name).order_by(Trace.started_at.desc())
        )
        assert trace is not None, name
        return trace


async def test_search_trace_has_retrieval_spans_with_hit_ids(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/v1/memories",
        json={"subject": "Tracing fixture", "content": "Tracer uses Postgres for traces."},
    )
    memory_id = created.json()["id"]
    drain_jobs()
    result = await client.post("/v1/search", json={"query": "Tracer Postgres traces"})
    assert result.status_code == 200
    trace = _latest("POST /v1/search")
    spans = {span["name"]: span for span in trace.spans}
    assert ["auth", "resolve_scope", "lexical", "vector", "graph", "fuse"] == [
        s["name"] for s in trace.spans
    ]
    assert trace.status == "ok"
    assert trace.http_status == 200
    assert trace.organization_id is not None
    assert trace.summary["query"] == "Tracer Postgres traces"
    assert trace.summary["hits"] == len(result.json()["hits"])
    lexical_ids = {c["id"] for c in spans["lexical"]["detail"]["candidates"]}
    assert memory_id in lexical_ids
    fused = {h["id"]: h for h in spans["fuse"]["detail"]["hits"]}
    assert "lexical" in fused[memory_id]["rrf"]
    assert spans["vector"]["detail"]["embedding_model"].startswith("hashing-v1")


async def test_traces_never_contain_memory_content(client: httpx.AsyncClient) -> None:
    marker = "ZEBRA-QUOKKA-LANTERN"
    await client.post("/v1/memories", json={"subject": marker, "content": f"{marker} body"})
    drain_jobs()
    await client.post("/v1/search", json={"query": "lantern"})
    get_trace_writer().flush()
    with SessionLocal() as session:
        rows = session.scalars(select(Trace)).all()
    assert all(marker not in json.dumps([t.summary, t.spans]) for t in rows)


async def test_unauthenticated_request_is_traced() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as anon:
        response = await anon.post("/v1/search", json={"query": "x"})
    assert response.status_code == 401
    trace = _latest("POST /v1/search")
    assert trace.status == "error"
    assert trace.http_status == 401
    assert trace.organization_id is None


async def test_enrichment_job_is_traced(client: httpx.AsyncClient) -> None:
    await client.post("/v1/memories", json={"content": "Job tracer uses Chunker."})
    drain_jobs()
    trace = _latest("enrich_memory")
    names = [s["name"] for s in trace.spans]
    assert names == ["classify", "chunk", "embed", "write_edges"]
    assert trace.kind == "job"
    assert trace.spans[1]["detail"]["chunks"] == 1
    assert trace.spans[3]["detail"]["edges_written"] == 1


async def test_health_endpoints_are_not_traced(client: httpx.AsyncClient) -> None:
    get_trace_writer().flush()
    with SessionLocal() as session:
        before = session.scalars(select(Trace).where(Trace.name.like("%health%"))).all()
    await client.get("/v1/health/live")
    get_trace_writer().flush()
    with SessionLocal() as session:
        after = session.scalars(select(Trace).where(Trace.name.like("%health%"))).all()
    assert len(after) == len(before)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tracing.py -q`
Expected: FAIL (`ModuleNotFoundError: albert.tracing` is not needed by the test file itself; failures are `AssertionError ... name` in `_latest` because no traces are written).

- [ ] **Step 3: Middleware and job helper**

```python
# src/albert/tracing.py
"""Wire the recorder around HTTP requests and worker jobs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from albert.models import Job
from albert.recorder import Recorder, activate
from albert.trace_writer import get_trace_writer

UNTRACED_PATH_PREFIXES = ("/v1/health", "/docs", "/openapi.json", "/redoc")


def _route_name(scope: dict) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None) or scope.get("path", "")
    return f"{scope.get('method', 'GET')} {path}"


class RecordingMiddleware:
    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope.get("path", "").startswith(UNTRACED_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return
        recorder = Recorder("request", f"{scope['method']} {scope['path']}")

        async def recording_send(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                recorder.http_status = message["status"]
            await send(message)

        with activate(recorder):
            try:
                await self.app(scope, receive, recording_send)
            except Exception:
                recorder.http_status = recorder.http_status or 500
                raise
            finally:
                # Routing has happened by now; prefer the template path to keep
                # names stable across ids.
                recorder.name = _route_name(scope)
                status = recorder.status
                if status is None:
                    code = recorder.http_status or 500
                    status = "error" if code >= 400 else "ok"
                get_trace_writer().submit(recorder.finish(status=status))


@contextmanager
def traced_job(job: Job) -> Iterator[Recorder]:
    recorder = Recorder("job", job.job_type)
    recorder.set_identity(job.organization_id, None)
    recorder.note(job_id=str(job.id), attempt=job.attempts, **job.payload)
    with activate(recorder):
        try:
            yield recorder
        except Exception:
            get_trace_writer().submit(recorder.finish(status="error"))
            raise
        get_trace_writer().submit(recorder.finish())
```

Install in `src/albert/api.py` right after `app = FastAPI(...)`:

```python
from albert.tracing import RecordingMiddleware

app.add_middleware(RecordingMiddleware)
```

Note: `scope["route"]` is set by Starlette's router only for `Mount`/sub-apps; for plain routes use `scope.get("endpoint")`. If `_route_name` yields the raw path with ids, that is acceptable for slice one: the test only asserts `POST /v1/search`. Keep `_route_name` but fall back to `scope["path"]`.

- [ ] **Step 4: Instrumentation points**

`src/albert/security.py` — wrap the body of `authenticate` after the bearer check:

```python
from albert.recorder import current_recorder, span
...
    with span("auth") as handle:
        parsed = parse_api_key(credentials.credentials)
        ... existing lookup/validation ...
        handle.set(prefix=prefix, capabilities=sorted(record.capabilities or []))
        recorder = current_recorder()
        if recorder is not None:
            recorder.set_identity(principal.organization_id, principal.id)
```

(The 401 raises inside the span mark it `error`, which is what Review Focus item 1 expects.)

`src/albert/services.py` — `resolve_workspace`:

```python
    with span("resolve_scope", requested=str(requested) if requested else None) as handle:
        ... existing body ...
        handle.set(workspace_id=str(workspace_id) if workspace_id else None)
        return workspace_id
```

`index_memory_chunks`:

```python
    with span("chunk") as chunk_span:
        chunks = chunk_text(...)
        truncated = ...
        chunks = chunks[: settings.max_chunks_per_memory]
        chunk_span.set(chunks=len(chunks), truncated=truncated, max_characters=settings.chunk_characters)
    with span("embed", embedding_model=embedder.name) as embed_span:
        for index, chunk in enumerate(chunks): ... session.add(MemoryChunk(...))
        embed_span.set(vectors=len(chunks), dimensions=embedder.dimensions)
```

`enrich_memory` and `enrich_episode`: wrap `classify(...)` in `with span("classify", provider=get_settings().llm_provider) as h:` and after it `h.set(memory_type=result.memory_type, sensitivity=result.sensitivity, entities=len(result.entities), relationships=len(result.relationships))`. Wrap the edge-closing and `add_relationship` loop in `with span("write_edges") as h:` counting `edges_closed` and `edges_written` (sum of `created` from `add_relationship`).

`src/albert/search.py` — in `hybrid_search`, wrap each backend call and the fusion loop:

```python
    with span("lexical") as handle:
        lexical = _lexical_search(...)
        handle.set(
            candidates_total=len(lexical),
            candidates=[
                {"id": str(m.id), "kind": "memory", "score": s, "rank": i}
                for i, (m, s) in enumerate(lexical, start=1)
            ][:CANDIDATE_LIMIT],
            filters=_filters(request, workspace_id),
        )
```

Same for `vector` (add `embedding_model=get_embedder().name`, `min_similarity=get_settings().min_vector_similarity`) and `graph` (`kind: "relationship"`, score = confidence). The `try/except` that appends to `degraded` stays outside the `with span(...)` so the span records `error` and the request continues; after the loop, if `degraded`, set `recorder.status = "degraded"` via `current_recorder()`. Fusion:

```python
    with span("fuse", k=RRF_K) as handle:
        ... existing fusion loop, additionally collecting per-key {backend: 1/(k+rank)} ...
        handle.set(hits=[
            {"id": key.split(":", 1)[1], "rank": i, "rrf": contributions[key], "backends": backend_names[key]}
            for i, key in enumerate(ordered, start=1)
        ])
```

Add `_filters(request, workspace_id) -> dict` returning `{"workspace_id", "project_ref", "task_ref", "run_ref", "memory_types", "sensitivity", "temporal_as_of"}` as strings/lists.

`src/albert/api.py` — in `search_memories`, `assemble_context`, `query_graph`: before calling, `recorder = current_recorder(); if recorder: recorder.note(query=data.query)`; after, `recorder.note(hits=len(result.hits), degraded=result.degraded)` (graph: `hits=len(result)`).

`src/albert/worker.py` — in `process_job` wrap the dispatch:

```python
from albert.tracing import traced_job
...
    try:
        with traced_job(job):
            if job.job_type == "enrich_memory": ...
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass including the 5 new tests. If `test_endpoints_do_not_run_blocking_io_on_the_event_loop` fails, a handler was made `async`; revert it.

- [ ] **Step 6: Commit**

```bash
git add src/albert/tracing.py src/albert/api.py src/albert/security.py src/albert/services.py src/albert/search.py src/albert/worker.py tests/test_tracing.py
git commit -m "feat(recorder): trace every request and job with retrieval candidates and fusion math"
```

---

### Task 5: Retention housekeeping and overhead benchmark

**Files:**
- Modify: `src/albert/worker.py:housekeeping`
- Test: `tests/test_tracing.py` (append), `tests/test_recorder_overhead.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracing.py`:

```python
def test_housekeeping_prunes_old_traces() -> None:
    from datetime import UTC, datetime, timedelta

    from albert.worker import housekeeping

    with SessionLocal() as session:
        old = Trace(kind="request", name="old", status="ok", started_at=datetime.now(UTC) - timedelta(days=30), duration_ms=1, summary={}, spans=[])
        fresh = Trace(kind="request", name="fresh", status="ok", started_at=datetime.now(UTC), duration_ms=1, summary={}, spans=[])
        session.add_all([old, fresh])
        session.commit()
        housekeeping(session)
        assert session.get(Trace, old.id) is None
        assert session.get(Trace, fresh.id) is not None
```

Create `tests/test_recorder_overhead.py`:

```python
from __future__ import annotations

import statistics
import time

import httpx
import pytest

from albert import recorder
from albert.api import app
from albert.trace_writer import get_trace_writer

pytestmark = pytest.mark.anyio


async def _median_ms(client: httpx.AsyncClient, rounds: int) -> float:
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        response = await client.post("/v1/search", json={"query": "overhead probe", "include_graph": False})
        assert response.status_code == 200
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


async def test_recording_overhead_is_within_budget(client: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await _median_ms(client, 5)  # warm up
    with_recording = await _median_ms(client, 40)
    get_trace_writer().flush()
    monkeypatch.setattr(recorder, "activate", lambda r: __import__("contextlib").nullcontext(r))
    monkeypatch.setattr("albert.tracing.activate", lambda r: __import__("contextlib").nullcontext(r))
    without = await _median_ms(client, 40)
    overhead = with_recording - without
    assert overhead < max(2.0, without * 0.05), f"{with_recording=:.2f} {without=:.2f}"
```

(With `activate` replaced by a null context the spans become no-ops because no recorder is active, while the middleware still runs; this isolates span capture plus submit cost.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tracing.py::test_housekeeping_prunes_old_traces tests/test_recorder_overhead.py -q`
Expected: housekeeping test FAILS (old trace still present); overhead test may pass already — that is acceptable for a budget guard, note it in the commit.

- [ ] **Step 3: Implement retention**

In `src/albert/worker.py:housekeeping`, before `session.commit()`:

```python
    retention = timedelta(days=get_settings().trace_retention_days)
    session.query(Trace).filter(Trace.started_at < now - retention).delete(synchronize_session=False)
```

Import `Trace` from `albert.models`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/albert/worker.py tests/test_tracing.py tests/test_recorder_overhead.py
git commit -m "feat(recorder): prune traces by retention; guard recording overhead in CI"
```

---

### Task 6: Console read API (traces, graph snapshot, overview, detail reads)

**Files:**
- Create: `src/albert/console.py` (APIRouter)
- Modify: `src/albert/api.py` (include router)
- Modify: `src/albert/admin.py` (`console.read` in `ALL_CAPABILITIES`)
- Modify: `src/albert/schemas.py` (response models)
- Test: `tests/test_console_api.py`

**Interfaces:**
- Produces router at `/v1/console`:
  - `GET /traces?kind&status&organization_id&principal_id&name&since&until&q&cursor&limit(≤200)` → `{"items": [TraceSummary], "next_cursor": str | None}`; cursor is `"<started_at iso>|<id>"`.
  - `GET /traces/{trace_id}` → `TraceRead` (with spans).
  - `GET /graph?organization_id&workspace_id&temporal_as_of&limit(default 2000, ≤5000)` → `{"nodes": [{"id","kind":"entity"|"memory","label","type","sensitivity","workspace_id"}], "edges": [{"id","source","target","relation_type","sensitivity","valid_from","valid_until","source_memory_id"}], "truncated": bool}`; memory nodes' `label` is `memory_type`, never subject.
  - `GET /overview` → `{"traces_per_minute": [{"minute","count","errors","degraded"}] (60 buckets), "jobs": {status: count}, "oldest_pending_seconds": float | None, "trace_count": int, "traces_dropped": int, "worker_last_seen": iso | None}`.
  - `GET /memories/{id}`, `GET /entities/{id}` → operator detail with `content` truncated to 500 chars; audited `memory.read`.
- Capability check: `auth.require("console.read")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_console_api.py
from __future__ import annotations

import httpx
import pytest

from albert.trace_writer import get_trace_writer

from .conftest import create_identity, create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio


def _console_client(key: str) -> httpx.AsyncClient:
    from albert.api import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    )


@pytest.fixture()
def console_key(identity):  # type: ignore[no-untyped-def]
    _key, organization, workspace = identity
    return create_principal_key(organization, None, "Console", ["console.read", "memory.read"])


async def test_console_requires_capability(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/console/traces")).status_code == 403


async def test_trace_listing_filters_and_paginates(client: httpx.AsyncClient, console_key: str) -> None:
    for i in range(3):
        await client.post("/v1/search", json={"query": f"page probe {i}"})
    get_trace_writer().flush()
    async with _console_client(console_key) as console:
        page = await console.get("/v1/console/traces", params={"name": "POST /v1/search", "limit": 2})
        assert page.status_code == 200, page.text
        body = page.json()
        assert len(body["items"]) == 2 and body["next_cursor"]
        assert "spans" not in body["items"][0]
        rest = await console.get("/v1/console/traces", params={"name": "POST /v1/search", "limit": 2, "cursor": body["next_cursor"]})
        assert rest.json()["items"][0]["id"] not in {i["id"] for i in body["items"]}
        detail = await console.get(f"/v1/console/traces/{body['items'][0]['id']}")
        assert detail.status_code == 200
        assert [s["name"] for s in detail.json()["spans"]][:2] == ["auth", "resolve_scope"]
        searched = await console.get("/v1/console/traces", params={"q": "page probe 1"})
        assert any(i["summary"]["query"] == "page probe 1" for i in searched.json()["items"])


async def test_graph_snapshot_is_capped(client: httpx.AsyncClient, console_key: str, identity) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    await client.post("/v1/memories", json={"content": "Snapshot Service uses Snapshot Store."})
    drain_jobs()
    async with _console_client(console_key) as console:
        too_big = await console.get("/v1/console/graph", params={"organization_id": str(organization.id), "limit": 5001})
        assert too_big.status_code == 422
        snapshot = await console.get("/v1/console/graph", params={"organization_id": str(organization.id), "limit": 1})
        body = snapshot.json()
        assert body["truncated"] is True
        assert len(body["nodes"]) == 1
        full = await console.get("/v1/console/graph", params={"organization_id": str(organization.id)})
        kinds = {n["kind"] for n in full.json()["nodes"]}
        assert {"entity", "memory"} <= kinds
        assert all("Snapshot Service uses" not in (n["label"] or "") for n in full.json()["nodes"] if n["kind"] == "memory")
        assert any(e["relation_type"] == "uses" for e in full.json()["edges"])


async def test_overview_reports_jobs_and_traces(client: httpx.AsyncClient, console_key: str) -> None:
    await client.post("/v1/search", json={"query": "overview probe"})
    get_trace_writer().flush()
    async with _console_client(console_key) as console:
        overview = await console.get("/v1/console/overview")
        body = overview.json()
        assert len(body["traces_per_minute"]) == 60
        assert body["trace_count"] >= 1
        assert isinstance(body["jobs"], dict)
        assert "traces_dropped" in body


async def test_operator_detail_reads_truncate_content(client: httpx.AsyncClient, console_key: str) -> None:
    created = await client.post("/v1/memories", json={"subject": "Detail", "content": "d" * 2000})
    async with _console_client(console_key) as console:
        detail = await console.get(f"/v1/console/memories/{created.json()['id']}")
        assert detail.status_code == 200
        assert len(detail.json()["content"]) == 500
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_console_api.py -q`
Expected: FAIL with 404s (router missing) and fixture errors for `console.read` unknown? (capabilities are free-form in `APIKey`; no error) — expect `assert 404 == 403` style failures.

- [ ] **Step 3: Implement the router**

```python
# src/albert/console.py
"""Operator read API for the memory console."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from albert.config import get_settings
from albert.db import get_session
from albert.models import Entity, Job, Memory, Relationship, Trace
from albert.security import AuthContext, authenticate
from albert.services import audit
from albert.trace_writer import get_trace_writer

router = APIRouter(prefix="/v1/console", tags=["console"])
GRAPH_NODE_CAP = 5000
_graph_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _operator(auth: AuthContext = Depends(authenticate)) -> AuthContext:
    auth.require("console.read")
    return auth


def _trace_summary(trace: Trace) -> dict[str, Any]:
    return {
        "id": str(trace.id), "kind": trace.kind, "name": trace.name,
        "organization_id": str(trace.organization_id) if trace.organization_id else None,
        "principal_id": str(trace.principal_id) if trace.principal_id else None,
        "status": trace.status, "http_status": trace.http_status,
        "started_at": trace.started_at.isoformat(), "duration_ms": trace.duration_ms,
        "summary": trace.summary,
    }


@router.get("/traces")
def list_traces(
    kind: str | None = None, status: str | None = None,
    organization_id: UUID | None = None, principal_id: UUID | None = None,
    name: str | None = None, since: datetime | None = None, until: datetime | None = None,
    q: str | None = None, cursor: str | None = None, limit: int = Query(default=50, ge=1, le=200),
    _auth: AuthContext = Depends(_operator), session: Session = Depends(get_session),
) -> dict[str, Any]:
    statement = select(Trace)
    for column, value in ((Trace.kind, kind), (Trace.status, status), (Trace.organization_id, organization_id), (Trace.principal_id, principal_id), (Trace.name, name)):
        if value is not None:
            statement = statement.where(column == value)
    if since is not None:
        statement = statement.where(Trace.started_at >= since)
    if until is not None:
        statement = statement.where(Trace.started_at <= until)
    if q:
        pattern = f"%{q}%"
        statement = statement.where(or_(Trace.name.ilike(pattern), Trace.summary["query"].as_string().ilike(pattern)))
    if cursor:
        try:
            raw_time, raw_id = cursor.split("|", 1)
            cursor_time = datetime.fromisoformat(raw_time)
            cursor_id = UUID(raw_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid cursor") from exc
        statement = statement.where(or_(Trace.started_at < cursor_time, (Trace.started_at == cursor_time) & (Trace.id < cursor_id)))
    rows = session.scalars(statement.order_by(Trace.started_at.desc(), Trace.id.desc()).limit(limit + 1)).all()
    items = rows[:limit]
    next_cursor = f"{items[-1].started_at.isoformat()}|{items[-1].id}" if len(rows) > limit else None
    return {"items": [_trace_summary(t) for t in items], "next_cursor": next_cursor}


@router.get("/traces/{trace_id}")
def get_trace(trace_id: UUID, _auth: AuthContext = Depends(_operator), session: Session = Depends(get_session)) -> dict[str, Any]:
    trace = session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {**_trace_summary(trace), "spans": trace.spans}


@router.get("/graph")
def graph_snapshot(
    organization_id: UUID, workspace_id: UUID | None = None, temporal_as_of: datetime | None = None,
    limit: int = Query(default=2000, ge=1, le=GRAPH_NODE_CAP),
    _auth: AuthContext = Depends(_operator), session: Session = Depends(get_session),
) -> dict[str, Any]:
    key = f"{organization_id}|{workspace_id}|{temporal_as_of}|{limit}"
    ttl = get_settings().console_graph_cache_seconds
    cached = _graph_cache.get(key)
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    when = temporal_as_of or datetime.now(UTC)
    scope = [Entity.organization_id == organization_id]
    if workspace_id is not None:
        scope.append(Entity.workspace_id == workspace_id)
    entities = session.scalars(select(Entity).where(*scope).order_by(Entity.created_at.desc()).limit(limit + 1)).all()
    truncated = len(entities) > limit
    entities = entities[:limit]
    entity_ids = {e.id for e in entities}
    nodes = [{"id": str(e.id), "kind": "entity", "label": e.display_name, "type": e.entity_type, "sensitivity": None, "workspace_id": str(e.workspace_id) if e.workspace_id else None} for e in entities]
    edges: list[dict[str, Any]] = []
    memory_ids: set[UUID] = set()
    if entity_ids:
        rels = session.scalars(select(Relationship).where(
            Relationship.organization_id == organization_id,
            Relationship.source_entity_id.in_(entity_ids), Relationship.target_entity_id.in_(entity_ids),
            Relationship.valid_from <= when, or_(Relationship.valid_until.is_(None), Relationship.valid_until > when),
        )).all()
        for r in rels:
            edges.append({"id": str(r.id), "source": str(r.source_entity_id), "target": str(r.target_entity_id), "relation_type": r.relation_type, "sensitivity": r.sensitivity, "valid_from": r.valid_from.isoformat(), "valid_until": r.valid_until.isoformat() if r.valid_until else None, "source_memory_id": str(r.source_memory_id) if r.source_memory_id else None})
            if r.source_memory_id:
                memory_ids.add(r.source_memory_id)
    remaining = limit - len(nodes)
    if remaining > 0:
        memory_scope = [Memory.organization_id == organization_id, Memory.status == "active"]
        if workspace_id is not None:
            memory_scope.append(Memory.workspace_id == workspace_id)
        memories = session.scalars(select(Memory).where(*memory_scope).order_by(Memory.created_at.desc()).limit(remaining + 1)).all()
        truncated = truncated or len(memories) > remaining
        for m in memories[:remaining]:
            nodes.append({"id": str(m.id), "kind": "memory", "label": m.memory_type, "type": m.memory_type, "sensitivity": m.sensitivity, "workspace_id": str(m.workspace_id) if m.workspace_id else None})
            memory_ids.add(m.id)
        present = {n["id"] for n in nodes}
        for e in edges:
            if e["source_memory_id"] and e["source_memory_id"] in present:
                edges.append({"id": f"derived:{e['id']}", "source": e["source_memory_id"], "target": e["source"], "relation_type": "derived_from", "sensitivity": e["sensitivity"], "valid_from": e["valid_from"], "valid_until": e["valid_until"], "source_memory_id": None})
    else:
        truncated = True
    result = {"nodes": nodes, "edges": edges, "truncated": truncated}
    _graph_cache[key] = (time.monotonic(), result)
    return result


@router.get("/overview")
def overview(_auth: AuthContext = Depends(_operator), session: Session = Depends(get_session)) -> dict[str, Any]:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=59)
    recent = session.scalars(select(Trace).where(Trace.started_at >= start)).all()
    buckets = {(start + timedelta(minutes=i)).isoformat(): {"count": 0, "errors": 0, "degraded": 0} for i in range(60)}
    for t in recent:
        key = t.started_at.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
        if key in buckets:
            buckets[key]["count"] += 1
            if t.status == "error":
                buckets[key]["errors"] += 1
            if t.status == "degraded":
                buckets[key]["degraded"] += 1
    jobs = dict(session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all())
    oldest = session.scalar(select(func.min(Job.available_at)).where(Job.status == "pending"))
    last_job = session.scalar(select(func.max(Trace.started_at)).where(Trace.kind == "job"))
    return {
        "traces_per_minute": [{"minute": k, **v} for k, v in buckets.items()],
        "jobs": {k: int(v) for k, v in jobs.items()},
        "oldest_pending_seconds": (datetime.now(UTC) - oldest.replace(tzinfo=UTC)).total_seconds() if oldest else None,
        "trace_count": int(session.scalar(select(func.count(Trace.id))) or 0),
        "traces_dropped": get_trace_writer().dropped,
        "worker_last_seen": last_job.isoformat() if last_job else None,
    }


@router.get("/memories/{memory_id}")
def operator_memory(memory_id: UUID, auth: AuthContext = Depends(_operator), session: Session = Depends(get_session)) -> dict[str, Any]:
    memory = session.get(Memory, memory_id)
    if memory is None or memory.status == "deleted":
        raise HTTPException(status_code=404, detail="Memory not found")
    audit(session, auth, "memory.read", resource_type="memory", resource_id=str(memory.id), detail={"console": True})
    session.commit()
    return {"id": str(memory.id), "subject": memory.subject, "content": memory.content[:500], "memory_type": memory.memory_type, "sensitivity": memory.sensitivity, "workspace_id": str(memory.workspace_id) if memory.workspace_id else None, "organization_id": str(memory.organization_id), "valid_from": memory.valid_from.isoformat(), "valid_until": memory.valid_until.isoformat() if memory.valid_until else None, "embedding_model": memory.embedding_model, "metadata": memory.metadata_}


@router.get("/entities/{entity_id}")
def operator_entity(entity_id: UUID, _auth: AuthContext = Depends(_operator), session: Session = Depends(get_session)) -> dict[str, Any]:
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {"id": str(entity.id), "name": entity.display_name, "entity_type": entity.entity_type, "description": entity.description[:500], "confidence": entity.confidence, "organization_id": str(entity.organization_id), "workspace_id": str(entity.workspace_id) if entity.workspace_id else None}
```

In `src/albert/api.py`: `from albert.console import router as console_router` and `app.include_router(console_router)`. In `src/albert/admin.py` add `"console.read"` to `ALL_CAPABILITIES`. Reformat the router to the 100-column limit (ruff will flag; wrap arguments).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/albert/console.py src/albert/api.py src/albert/admin.py tests/test_console_api.py
git commit -m "feat(console): operator read API for traces, graph snapshot, overview"
```

---

### Task 7: Live feed (SSE)

**Files:**
- Create: `src/albert/trace_feed.py`
- Modify: `src/albert/console.py` (stream endpoint)
- Test: `tests/test_trace_feed.py`

**Interfaces:**
- Produces: `class TraceFeed(session_factory, engine)` with `events(after: tuple[datetime, UUID] | None, stop: threading.Event, poll_seconds=1.0) -> Iterator[dict]` yielding `{"id": str, "data": dict}` (data = `_trace_summary`). On PostgreSQL it opens a raw psycopg connection with `LISTEN albert_traces` and uses notifications as a wake signal (`conn.notifies(timeout=poll_seconds)`), then always queries rows newer than the cursor; on other dialects it sleeps `poll_seconds`. `GET /v1/console/stream` returns `text/event-stream`, honours `Last-Event-ID` (`"<iso>|<uuid>"`), emits `: keepalive` every 15 s.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trace_feed.py
from __future__ import annotations

import threading
from datetime import UTC, datetime

from albert.db import SessionLocal, engine
from albert.models import Trace
from albert.trace_feed import TraceFeed


def _insert(name: str) -> Trace:
    with SessionLocal() as session:
        trace = Trace(kind="request", name=name, status="ok", started_at=datetime.now(UTC), duration_ms=1, summary={}, spans=[])
        session.add(trace)
        session.commit()
        return trace


def test_feed_delivers_traces_inserted_after_subscription() -> None:
    feed = TraceFeed(SessionLocal, engine)
    stop = threading.Event()
    before = _insert("before-subscribe")
    events = feed.events(after=(before.started_at, before.id), stop=stop, poll_seconds=0.05)
    inserted = _insert("after-subscribe")
    event = next(events)
    stop.set()
    assert event["id"].endswith(str(inserted.id))
    assert event["data"]["name"] == "after-subscribe"


def test_feed_backfills_from_last_event_id() -> None:
    feed = TraceFeed(SessionLocal, engine)
    first = _insert("backfill-1")
    second = _insert("backfill-2")
    stop = threading.Event()
    events = feed.events(after=(first.started_at, first.id), stop=stop, poll_seconds=0.05)
    event = next(events)
    stop.set()
    assert event["data"]["name"] == "backfill-2"
    assert event["id"].endswith(str(second.id))
```

Append to `tests/test_console_api.py`:

```python
async def test_stream_endpoint_emits_sse(client: httpx.AsyncClient, console_key: str) -> None:
    await client.post("/v1/search", json={"query": "stream probe"})
    get_trace_writer().flush()
    async with _console_client(console_key) as console:
        async with console.stream("GET", "/v1/console/stream", headers={"Last-Event-ID": "1970-01-01T00:00:00+00:00|00000000-0000-0000-0000-000000000000"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            chunk = ""
            async for text in response.aiter_text():
                chunk += text
                if "\n\n" in chunk:
                    break
    assert chunk.startswith("id: ")
    assert "\ndata: {" in chunk
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_trace_feed.py tests/test_console_api.py::test_stream_endpoint_emits_sse -q`
Expected: FAIL with `ModuleNotFoundError: albert.trace_feed` and 404.

- [ ] **Step 3: Implement**

```python
# src/albert/trace_feed.py
"""Cursor-based trace feed; PostgreSQL notifications only wake the poll."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from albert.models import Trace
from albert.trace_writer import NOTIFY_CHANNEL

logger = logging.getLogger("albert.trace_feed")


def event_id(trace: Trace) -> str:
    return f"{trace.started_at.isoformat()}|{trace.id}"


def parse_event_id(value: str | None) -> tuple[datetime, UUID] | None:
    if not value:
        return None
    try:
        raw_time, raw_id = value.split("|", 1)
        return datetime.fromisoformat(raw_time), UUID(raw_id)
    except ValueError:
        return None


class TraceFeed:
    def __init__(self, session_factory: Callable[[], Session], engine: Engine) -> None:
        self._sessions = session_factory
        self._postgres = engine.dialect.name == "postgresql"
        self._engine = engine

    def _newer_than(self, after: tuple[datetime, UUID] | None, limit: int = 200) -> list[Trace]:
        with self._sessions() as session:
            statement = select(Trace)
            if after is not None:
                cursor_time, cursor_id = after
                statement = statement.where(
                    or_(Trace.started_at > cursor_time, (Trace.started_at == cursor_time) & (Trace.id > cursor_id))
                )
            return session.scalars(statement.order_by(Trace.started_at, Trace.id).limit(limit)).all()

    def _wait(self, stop: threading.Event, poll_seconds: float, listener: Any) -> None:
        if listener is None:
            stop.wait(poll_seconds)
            return
        try:
            for _notification in listener.notifies(timeout=poll_seconds, stop_after=1):
                break
        except Exception:
            logger.warning("LISTEN failed; falling back to polling", exc_info=True)
            stop.wait(poll_seconds)

    def events(self, after: tuple[datetime, UUID] | None, stop: threading.Event, poll_seconds: float = 1.0) -> Iterator[dict[str, Any]]:
        from albert.console import _trace_summary

        listener = None
        if self._postgres:
            try:
                listener = self._engine.raw_connection().driver_connection
                listener.autocommit = True
                listener.execute(f"LISTEN {NOTIFY_CHANNEL}")
            except Exception:
                logger.warning("could not LISTEN; polling instead", exc_info=True)
                listener = None
        cursor = after
        if cursor is None:
            latest = self._newer_than(None, limit=1)
            cursor = (latest[0].started_at, latest[0].id) if latest else None
            # Fresh subscription: start from "now"; only new rows flow.
            if cursor is None:
                cursor = (datetime.min.replace(tzinfo=None), UUID(int=0))
        try:
            while not stop.is_set():
                rows = self._newer_than(cursor)
                for trace in rows:
                    cursor = (trace.started_at, trace.id)
                    yield {"id": event_id(trace), "data": _trace_summary(trace)}
                if not rows:
                    self._wait(stop, poll_seconds, listener)
        finally:
            if listener is not None:
                listener.close()
```

Note on fresh subscriptions: when `after` is None, the feed starts at the newest existing row so only new traces flow; the SQLite comparison uses naive datetimes consistently because SQLite drops tzinfo — normalize `cursor_time` with `.replace(tzinfo=None)` when `not self._postgres`.

Add to `src/albert/console.py`:

```python
import json, threading
from fastapi import Request
from fastapi.responses import StreamingResponse
from albert.db import engine
from albert.trace_feed import TraceFeed, parse_event_id

_feed = TraceFeed(SessionLocal, engine)

@router.get("/stream")
def stream(request: Request, _auth: AuthContext = Depends(_operator)) -> StreamingResponse:
    after = parse_event_id(request.headers.get("last-event-id"))
    stop = threading.Event()

    def body():
        last_keepalive = time.monotonic()
        try:
            for event in _feed.events(after, stop):
                yield f"id: {event['id']}\nevent: trace\ndata: {json.dumps(event['data'])}\n\n"
                if time.monotonic() - last_keepalive > 15:
                    yield ": keepalive\n\n"
                    last_keepalive = time.monotonic()
        finally:
            stop.set()

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

(`StreamingResponse` iterates sync generators in a threadpool; the endpoint stays `def`.) Keepalives must also fire while idle: have `events()` yield `None` when a wait completes with no rows, and emit `: keepalive` for `None`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/albert/trace_feed.py src/albert/console.py tests/test_trace_feed.py tests/test_console_api.py
git commit -m "feat(console): server-sent trace feed with LISTEN/NOTIFY wake and poll fallback"
```

---

### Task 8: Console app scaffold, proxy and Live screen

**Files:**
- Create: `console/package.json`, `console/next.config.ts`, `console/tsconfig.json`, `console/.gitignore`, `console/app/layout.tsx`, `console/app/globals.css`, `console/app/page.tsx`, `console/app/api/albert/[...path]/route.ts`, `console/lib/api.ts`, `console/lib/types.ts`, `console/components/Feed.tsx`, `console/components/StatusPill.tsx`, `console/components/Nav.tsx`
- Test: `console/e2e/live.spec.ts`, `console/playwright.config.ts`

**Interfaces:**
- Env: `ALBERT_API_URL` (default `http://127.0.0.1:8080`), `ALBERT_CONSOLE_API_KEY`.
- Proxy: any method on `/api/albert/<path>?query` forwards to `${ALBERT_API_URL}/<path>?query` adding `Authorization: Bearer <key>`; streams the body (needed for SSE); passes status codes through.
- `lib/types.ts`: `TraceSummary`, `TraceDetail`, `Span`, `GraphSnapshot`, `GraphNode`, `GraphEdge`, `Overview` mirroring Task 6/7 JSON exactly.
- `lib/api.ts`: `listTraces(params)`, `getTrace(id)`, `getGraph(params)`, `getOverview()`, `getMemory(id)`, `getEntity(id)`, `openTraceStream(onEvent, lastEventId?) -> () => void` (uses `EventSource` against `/api/albert/v1/console/stream`).

- [ ] **Step 1: Scaffold**

```bash
cd console && npm init -y >/dev/null
npm install next@15 react@19 react-dom@19 react-force-graph-3d three
npm install -D typescript @types/react @types/react-dom @types/node @playwright/test eslint eslint-config-next
```

`console/package.json` scripts: `"dev": "next dev -p 8082"`, `"build": "next build"`, `"start": "next start -p 8082 -H 0.0.0.0"`, `"lint": "next lint"`, `"e2e": "playwright test"`.

`console/next.config.ts`: `output: "standalone"`.

- [ ] **Step 2: Write the failing Playwright test**

```ts
// console/e2e/live.spec.ts
import { expect, test } from "@playwright/test";

test("live feed shows a search trace", async ({ page, request }) => {
  // The e2e harness exports ALBERT_E2E_KEY (operator key with memory.* + console.read).
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080";
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Live" })).toBeVisible();
  const response = await request.post(`${api}/v1/search`, {
    headers: { Authorization: `Bearer ${process.env.ALBERT_E2E_KEY}` },
    data: { query: "playwright live probe" },
  });
  expect(response.ok()).toBeTruthy();
  const row = page.getByTestId("trace-row").filter({ hasText: "POST /v1/search" }).first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(row).toContainText("playwright live probe");
});
```

`console/playwright.config.ts`: `baseURL: "http://127.0.0.1:8082"`, `webServer: { command: "npm run dev", port: 8082, reuseExistingServer: true }`.

- [ ] **Step 3: Run it to verify it fails**

Start the API: `ALBERT_DATABASE_URL=sqlite:////tmp/albert-e2e.sqlite3 ALBERT_API_KEY_PEPPER=... .venv/bin/alembic upgrade head && .venv/bin/albert-admin bootstrap --organization E2E --workspace Default` (capture the key into `ALBERT_E2E_KEY` and `ALBERT_CONSOLE_API_KEY`), then `.venv/bin/uvicorn albert.api:app --port 8080 &` and `.venv/bin/albert-worker &`.
Run: `cd console && npx playwright test e2e/live.spec.ts`
Expected: FAIL (no heading "Live").

- [ ] **Step 4: Implement proxy, API client, layout, Live**

```ts
// console/app/api/albert/[...path]/route.ts
import { NextRequest } from "next/server";

const API = (process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
const KEY = process.env.ALBERT_CONSOLE_API_KEY ?? "";

async function proxy(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  const url = `${API}/${path.join("/")}${request.nextUrl.search}`;
  const headers = new Headers({ Authorization: `Bearer ${KEY}` });
  const accept = request.headers.get("accept");
  if (accept) headers.set("accept", accept);
  const lastEventId = request.headers.get("last-event-id");
  if (lastEventId) headers.set("last-event-id", lastEventId);
  if (request.headers.get("content-type")) headers.set("content-type", request.headers.get("content-type")!);
  const upstream = await fetch(url, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    // @ts-expect-error node fetch streaming body
    duplex: "half",
    cache: "no-store",
  });
  const out = new Headers();
  for (const name of ["content-type", "cache-control", "x-accel-buffering"]) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE };
export const dynamic = "force-dynamic";
```

```ts
// console/lib/types.ts
export type TraceStatus = "ok" | "error" | "degraded";
export interface TraceSummary { id: string; kind: "request" | "job"; name: string; organization_id: string | null; principal_id: string | null; status: TraceStatus; http_status: number | null; started_at: string; duration_ms: number; summary: Record<string, unknown> & { query?: string; hits?: number; degraded?: string[] }; }
export interface Span { name: string; seq: number; started_offset_ms: number; duration_ms: number; status: "ok" | "error"; detail: Record<string, unknown>; }
export interface TraceDetail extends TraceSummary { spans: Span[]; }
export interface Candidate { id: string; kind: "memory" | "relationship"; score: number; rank: number; }
export interface FusedHit { id: string; rank: number; rrf: Record<string, number>; backends: string[]; }
export interface GraphNode { id: string; kind: "entity" | "memory"; label: string; type: string; sensitivity: string | null; workspace_id: string | null; }
export interface GraphEdge { id: string; source: string; target: string; relation_type: string; sensitivity: string; valid_from: string; valid_until: string | null; source_memory_id: string | null; }
export interface GraphSnapshot { nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean; }
export interface Overview { traces_per_minute: { minute: string; count: number; errors: number; degraded: number }[]; jobs: Record<string, number>; oldest_pending_seconds: number | null; trace_count: number; traces_dropped: number; worker_last_seen: string | null; }
```

```ts
// console/lib/api.ts
import type { GraphSnapshot, Overview, TraceDetail, TraceSummary } from "./types";

const base = "/api/albert/v1/console";

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const query = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])).toString() : "";
  const response = await fetch(`${base}${path}${query}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json() as Promise<T>;
}

export const listTraces = (params: Record<string, string | number | undefined>) => get<{ items: TraceSummary[]; next_cursor: string | null }>("/traces", params);
export const getTrace = (id: string) => get<TraceDetail>(`/traces/${id}`);
export const getGraph = (params: { organization_id: string; workspace_id?: string; temporal_as_of?: string; limit?: number }) => get<GraphSnapshot>("/graph", params);
export const getOverview = () => get<Overview>("/overview");
export const getMemory = (id: string) => get<Record<string, unknown>>(`/memories/${id}`);
export const getEntity = (id: string) => get<Record<string, unknown>>(`/entities/${id}`);

export function openTraceStream(onEvent: (trace: TraceSummary) => void, lastEventId?: string): () => void {
  const url = `${base}/stream${lastEventId ? `?last=${encodeURIComponent(lastEventId)}` : ""}`;
  const source = new EventSource(url);
  source.addEventListener("trace", (event) => onEvent(JSON.parse((event as MessageEvent).data)));
  return () => source.close();
}
```

`console/components/Feed.tsx` (client component): on mount calls `listTraces({limit: 100})`, then `openTraceStream` prepending new rows (dedupe by id, cap 500). Each row: `<div data-testid="trace-row">` with `<StatusPill status>`, kind badge, `name`, `summary.query` (mono, truncated), backends from `summary.degraded`, `duration_ms` fixed(1) ms, relative time, organization short id. Row links to `/traces/${id}`. Filter bar: kind (all/request/job), status, name text — client-side filter on the loaded list plus refetch when changed.

`console/app/page.tsx`: `<h1>Live</h1>` + `<Feed />`. `console/app/layout.tsx`: `<Nav />` with links Live `/`, Explorer `/explorer`; dark theme tokens in `globals.css` (`--bg #0b0d10`, `--panel #12161c`, `--line #1f2630`, `--fg #e6edf3`, `--muted #8b98a5`, `--ok #3fb950`, `--warn #d29922`, `--err #f85149`, `--lexical #58a6ff`, `--vector #bc8cff`, `--graph #f0883e`, mono `ui-monospace`). Invoke the `frontend-design` skill before writing components.

- [ ] **Step 5: Run the Playwright test and lint**

Run: `cd console && npm run lint && npx playwright test e2e/live.spec.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add console
git commit -m "feat(console): Next.js console with server-held key proxy and live trace feed"
```

---

### Task 9: Replay screen

**Files:**
- Create: `console/app/traces/[id]/page.tsx`, `console/components/Waterfall.tsx`, `console/components/SpanDetail.tsx`, `console/components/FusionTable.tsx`
- Test: `console/e2e/replay.spec.ts`

**Interfaces:**
- Consumes `getTrace` (Task 8), `Span`, `FusedHit`, `Candidate` types.
- Produces: `Waterfall({ spans, selected, onSelect })` renders one row per span with a bar positioned by `started_offset_ms / total` and width by `duration_ms`, colored by name (`lexical`/`vector`/`graph` use their tokens; `error` spans red). `SpanDetail({ span })` renders the detail as key/value; for candidate lists, a table. `FusionTable({ fuse, lexical, vector, graph })` lists final hits with per-backend rank (from candidate lists) and RRF contribution, then "found by one backend only" lists. The page exposes a step scrubber (`<input type="range">`, data-testid `step-scrubber`) that sets `selected` and highlights the span; `ArrowLeft/ArrowRight` move it. Below, `<Explorer traceId=...>` placeholder slot that Task 10 fills.

- [ ] **Step 1: Write the failing Playwright test**

```ts
// console/e2e/replay.spec.ts
import { expect, test } from "@playwright/test";

test("replay walks spans and shows fusion", async ({ page, request }) => {
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080";
  const headers = { Authorization: `Bearer ${process.env.ALBERT_E2E_KEY}` };
  await request.post(`${api}/v1/memories`, { headers, data: { subject: "Replay fixture", content: "Replay Service uses Replay Store." } });
  await new Promise((r) => setTimeout(r, 3000)); // worker enrichment
  await request.post(`${api}/v1/search`, { headers, data: { query: "Replay Store" } });
  await page.goto("/");
  await page.getByTestId("trace-row").filter({ hasText: "Replay Store" }).first().click();
  await expect(page.getByRole("heading", { name: /POST \/v1\/search/ })).toBeVisible();
  const rows = page.getByTestId("span-row");
  await expect(rows).toHaveCount(6);
  await expect(rows.nth(2)).toContainText("lexical");
  await page.getByTestId("step-scrubber").fill("5");
  await expect(page.getByTestId("span-detail")).toContainText("fuse");
  await expect(page.getByTestId("fusion-table")).toContainText("lexical");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd console && npx playwright test e2e/replay.spec.ts`
Expected: FAIL (404 page).

- [ ] **Step 3: Implement the page and components**

Page (`app/traces/[id]/page.tsx`, client): loads `getTrace(id)`; header `<h1>{trace.name}</h1>` with status pill, duration, started time, organization/principal ids; two-column grid: left `Waterfall` + scrubber + `SpanDetail`, and when a `fuse` span exists `FusionTable`; right `<ExplorerPanel trace={trace} />` (Task 10; render `<div data-testid="explorer-slot" />` for now).

Waterfall row:

```tsx
<div data-testid="span-row" className={selected === span.seq ? "span selected" : "span"} onClick={() => onSelect(span.seq)}>
  <span className="span-name">{span.name}</span>
  <div className="track"><div className={`bar ${span.name} ${span.status}`} style={{ left: `${(span.started_offset_ms / total) * 100}%`, width: `${Math.max(0.5, (span.duration_ms / total) * 100)}%` }} /></div>
  <span className="mono">{span.duration_ms.toFixed(2)} ms</span>
</div>
```

FusionTable derives per-backend rank maps from the `lexical`/`vector`/`graph` spans' `detail.candidates` and renders rows `rank | id (short, link to detail) | lexical rank | vector rank | graph rank | rrf total` plus a "single-backend candidates" section listing ids that appear in exactly one backend's list and not in `hits`.

- [ ] **Step 4: Run the test and lint**

Run: `cd console && npm run lint && npx playwright test e2e/replay.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add console
git commit -m "feat(console): trace replay with span waterfall, scrubber and fusion table"
```

---

### Task 10: Explorer (3D graph with time slider and trace highlighting)

**Files:**
- Create: `console/app/explorer/page.tsx`, `console/components/Graph3D.tsx`, `console/components/ExplorerPanel.tsx`, `console/components/TimeSlider.tsx`, `console/components/DetailPanel.tsx`, `console/lib/highlight.ts`
- Modify: `console/app/traces/[id]/page.tsx` (replace slot with `ExplorerPanel`)
- Test: `console/e2e/explorer.spec.ts`

**Interfaces:**
- `lib/highlight.ts`: `export function highlightFromTrace(trace: TraceDetail): { lexical: Set<string>; vector: Set<string>; graph: Set<string>; hits: Set<string>; edgeHits: Set<string> }` — reads candidate ids from the three backend spans (`graph` candidates are relationship ids → `edgeHits`; their memory ids are not known client-side, so graph candidates light edges) and `fuse.detail.hits` ids → `hits`.
- `Graph3D({ snapshot, highlight?, onSelect })`: dynamic import of `react-force-graph-3d` with `ssr: false`; node color by kind and sensitivity; when `highlight` present, non-highlighted nodes dim to 25 % alpha, lexical/vector tints applied, hits pulse (size oscillates via `requestAnimationFrame`), highlighted edges widen; `onSelect(node)` opens `DetailPanel` which fetches `getMemory`/`getEntity`. Node label on hover: `label` (entity display name or memory type).
- `TimeSlider({ min, max, value, onChange })`: range over `[min(valid_from), now]` from the snapshot, debounced 300 ms, drives `temporal_as_of` refetch.
- `ExplorerPanel({ trace? })`: organization/workspace pickers (organization defaults to `trace.organization_id`), fetches snapshot via `getGraph`, shows `truncated` banner, hosts `TimeSlider`, `Graph3D` and `DetailPanel`; on mount with a trace, calls `fg.zoomToFit` after highlights apply (expose via `onEngineStop`).
- Explorer page: `<h1>Explorer</h1>` + `<ExplorerPanel />` without trace.

- [ ] **Step 1: Write the failing Playwright test**

```ts
// console/e2e/explorer.spec.ts
import { expect, test } from "@playwright/test";

test("explorer renders a graph and a trace highlights hits", async ({ page, request }) => {
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080";
  const headers = { Authorization: `Bearer ${process.env.ALBERT_E2E_KEY}` };
  await request.post(`${api}/v1/memories`, { headers, data: { subject: "Explorer fixture", content: "Explorer Service uses Explorer Store." } });
  await new Promise((r) => setTimeout(r, 3000));
  await request.post(`${api}/v1/search`, { headers, data: { query: "Explorer Store" } });
  await page.goto("/explorer");
  await expect(page.getByRole("heading", { name: "Explorer" })).toBeVisible();
  await expect(page.getByTestId("graph-canvas").locator("canvas")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("graph-node-count")).not.toHaveText("0");
  await page.goto("/");
  await page.getByTestId("trace-row").filter({ hasText: "Explorer Store" }).first().click();
  await expect(page.getByTestId("highlight-legend")).toContainText("lexical");
  await expect(page.getByTestId("highlight-hit-count")).not.toHaveText("0");
  await page.getByTestId("time-slider").fill("0");
  await expect(page.getByTestId("graph-edge-count")).toHaveText("0");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd console && npx playwright test e2e/explorer.spec.ts`
Expected: FAIL (404 page).

- [ ] **Step 3: Implement**

```ts
// console/lib/highlight.ts
import type { Candidate, FusedHit, TraceDetail } from "./types";

export interface Highlight { lexical: Set<string>; vector: Set<string>; graph: Set<string>; hits: Set<string>; edgeHits: Set<string>; }

export function highlightFromTrace(trace: TraceDetail): Highlight {
  const ids = (name: string) => new Set(((trace.spans.find((s) => s.name === name)?.detail.candidates as Candidate[] | undefined) ?? []).map((c) => c.id));
  const hits = ((trace.spans.find((s) => s.name === "fuse")?.detail.hits as FusedHit[] | undefined) ?? []);
  return {
    lexical: ids("lexical"),
    vector: ids("vector"),
    graph: ids("graph"),
    hits: new Set(hits.filter((h) => !ids("graph").has(h.id)).map((h) => h.id)),
    edgeHits: new Set(hits.filter((h) => ids("graph").has(h.id)).map((h) => h.id)),
  };
}
```

`Graph3D.tsx` key props for `ForceGraph3D`: `graphData={{ nodes, links }}`, `nodeId="id"`, `nodeLabel={(n) => `${n.kind}: ${n.label}`}`, `nodeColor={(n) => colorFor(n, highlight, pulse)}`, `nodeVal={(n) => highlight?.hits.has(n.id) ? 6 + 3 * Math.sin(pulse) : n.kind === "entity" ? 3 : 2}`, `linkColor={(l) => highlight?.edgeHits.has(l.id) ? tokens.graph : "rgba(139,152,165,0.35)"}`, `linkWidth={(l) => highlight?.edgeHits.has(l.id) ? 2.5 : 0.6}`, `linkDirectionalParticles={(l) => highlight?.edgeHits.has(l.id) ? 4 : 0}`, `onNodeClick={onSelect}`, `backgroundColor="#0b0d10"`. `pulse` advances in a `requestAnimationFrame` loop only while a highlight exists (`setPulse` every frame is acceptable at this scale; throttle to 30 fps). Wrap in `<div data-testid="graph-canvas">` and render `<span data-testid="graph-node-count">{nodes.length}</span>` and `<span data-testid="graph-edge-count">{links.length}</span>` in a stats strip with the `truncated` banner.

`colorFor`: entity `#8b98a5`, memory by sensitivity (`public #3fb950`, `internal #58a6ff`, `confidential #d29922`, `restricted #f85149`); with highlight: in `hits` → `#ffffff`; in both lexical and vector → blend `#8ad0ff`; lexical only → `--lexical`; vector only → `--vector`; else dim `rgba(139,152,165,0.25)`.

`TimeSlider` emits ISO `temporal_as_of`; `data-testid="time-slider"`; position 0 maps to `min(valid_from) - 1s` so every edge is excluded (test expects edge count 0).

`ExplorerPanel` legend: `<div data-testid="highlight-legend">` with swatches for lexical, vector, graph, hit and `<span data-testid="highlight-hit-count">{hits.size}</span>`.

- [ ] **Step 4: Run tests and lint**

Run: `cd console && npm run lint && npx playwright test`
Expected: all three specs PASS.

- [ ] **Step 5: Commit**

```bash
git add console
git commit -m "feat(console): 3D memory explorer with temporal slider and trace retrieval highlighting"
```

---

### Task 11: Packaging, compose, CI and docs

**Files:**
- Create: `console/Dockerfile`, `console/e2e/run.sh`
- Modify: `compose.yaml`, `.github/workflows/ci.yml`, `docs/DEPLOYMENT.md`, `docs/API_AND_MCP.md`, `docs/STATUS.md`, `docs/SECURITY.md`, `README.md`, `.gitignore`

- [ ] **Step 1: Console Dockerfile**

```dockerfile
# console/Dockerfile
FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production PORT=8082 HOSTNAME=0.0.0.0
RUN groupadd --system console && useradd --system --gid console --home /app console
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER console
EXPOSE 8082
CMD ["node", "server.js"]
```

- [ ] **Step 2: Compose service**

Append to `compose.yaml` services:

```yaml
  console:
    build: ./console
    environment:
      ALBERT_API_URL: http://api:8080
      ALBERT_CONSOLE_API_KEY: ${ALBERT_CONSOLE_API_KEY:?set ALBERT_CONSOLE_API_KEY (albert-admin create-key --capabilities console.read)}
    ports:
      - "${ALBERT_CONSOLE_BIND:-127.0.0.1}:${ALBERT_CONSOLE_PORT:-8082}:8082"
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped
```

Add `ALBERT_TRACE_SAMPLE_RATE: ${ALBERT_TRACE_SAMPLE_RATE:-1.0}` and `ALBERT_TRACE_RETENTION_DAYS: ${ALBERT_TRACE_RETENTION_DAYS:-14}` to the shared `albert-env` block.

- [ ] **Step 3: e2e harness script**

```bash
# console/e2e/run.sh — starts a SQLite API + worker, bootstraps a key, runs Playwright
#!/bin/sh
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
db=$(mktemp -t albert-e2e.XXXXXX).sqlite3
export ALBERT_DATABASE_URL="sqlite:///$db"
export ALBERT_API_KEY_PEPPER=e2e-pepper-that-is-longer-than-thirty-two-characters
export ALBERT_EMBEDDING_PROVIDER=hashing ALBERT_EMBEDDING_DIMENSIONS=64
cd "$root"
.venv/bin/alembic upgrade head >/dev/null
key=$(.venv/bin/albert-admin bootstrap --organization E2E --workspace Default | tail -1)
export ALBERT_E2E_KEY="$key" ALBERT_CONSOLE_API_KEY="$key" ALBERT_API_URL=http://127.0.0.1:8080
.venv/bin/uvicorn albert.api:app --port 8080 --log-level warning & api=$!
.venv/bin/albert-worker & worker=$!
trap 'kill $api $worker 2>/dev/null; rm -f "$db"' EXIT
sleep 2
cd console && npx playwright test "$@"
```

- [ ] **Step 4: CI job**

Add a `console` job to `.github/workflows/ci.yml`:

```yaml
  console:
    runs-on: ubuntu-latest
    needs: test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: python -m venv .venv && .venv/bin/pip install --constraint constraints.txt -e '.[dev]'
      - uses: actions/setup-node@v4
        with: { node-version: "22", cache: npm, cache-dependency-path: console/package-lock.json }
      - run: cd console && npm ci && npm run lint && npm run build
      - run: cd console && npx playwright install --with-deps chromium
      - run: sh console/e2e/run.sh
      - run: docker build --tag albert-console:ci console
```

- [ ] **Step 5: Docs**

`docs/DEPLOYMENT.md`: new section "Memory console" — issue the console key (`albert-admin create-principal --organization <org> --name "Console" --principal-type service`, then `create-key --principal-id <id> --capabilities console.read,memory.read --name console`), set `ALBERT_CONSOLE_API_KEY`, `docker compose up -d console`, open `http://127.0.0.1:8082`, loopback/TLS rule, trace settings and retention. `docs/API_AND_MCP.md`: "Console API" section listing the six endpoints, the `console.read` capability, the SSE contract (`id`, `event: trace`, `Last-Event-ID`). `docs/STATUS.md`: add "Flight recorder traces for every request and job", "Live trace feed (SSE)", "Operator console: Live, Replay, 3D Explorer". `docs/SECURITY.md`: traces hold ids/scores/query text only; console key is server-held; operators see all tenants; console detail reads are audited. `README.md`: one paragraph and the console URL. `.gitignore`: `console/node_modules/`, `console/.next/`, `console/test-results/`, `console/playwright-report/`.

- [ ] **Step 6: Full verification**

Run: `.venv/bin/ruff check . && .venv/bin/pytest -q && sh console/e2e/run.sh && docker compose config --quiet` (compose needs the env vars set; CI covers it if Docker is unavailable locally).
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add console/Dockerfile console/e2e/run.sh compose.yaml .github/workflows/ci.yml docs README.md .gitignore
git commit -m "feat(console): package console service, CI job, deployment and API docs"
```

---

## Self-review notes

- Spec coverage: recorder (T1–T5), live feed (T7), console API (T6), console app Live/Replay/Explorer (T8–T10), deployment/CI/docs (T11), performance budget (T5 benchmark, T6 cache/cap, T3 queue). Retention: T5. `Last-Event-ID`: T7. Operator detail reads audited: T6.
- Type consistency: span names and candidate/hit shapes are defined once in Task 4's Interfaces and reused verbatim in T6, T8 types and T10 `highlight.ts`. Cursor format `"<iso>|<uuid>"` shared by T6 list and T7 feed.
- Review Focus items map to tests in T4 (1), T3 (2), T1 (3), T7 (4), T6 (5).

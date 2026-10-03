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


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def _cap_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Shrink a detail dict until it serializes under SPAN_DETAIL_LIMIT."""
    capped = dict(detail)
    truncated = False
    for key, value in list(capped.items()):
        if isinstance(value, list) and len(value) > CANDIDATE_LIMIT:
            capped[key] = value[:CANDIDATE_LIMIT]
            truncated = True
    while capped and _size(capped) > SPAN_DETAIL_LIMIT:
        truncated = True
        largest = max(capped, key=lambda k: _size(capped[k]))
        value = capped[largest]
        if isinstance(value, list) and len(value) > 1:
            capped[largest] = value[: len(value) // 2]
        elif isinstance(value, str) and len(value) > 64:
            capped[largest] = value[: max(16, len(value) // 2)]
        else:
            del capped[largest]
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
        if isinstance(fields.get("query"), str):
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
        # The message can embed SQL parameters or constraint values, which may
        # be memory content; the type (and HTTP status) is all a trace keeps.
        handle.set(error=type(exc).__name__)
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            handle.set(status_code=status_code)
        recorder.add_span(name, started, handle.detail, "error")
        raise
    recorder.add_span(name, started, handle.detail, "ok")

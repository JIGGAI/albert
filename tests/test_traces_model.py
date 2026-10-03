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

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
    assert [s["name"] for s in trace.spans] == [
        "auth",
        "resolve_scope",
        "lexical",
        "vector",
        "graph",
        "fuse",
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as anon:
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
    assert [s["name"] for s in trace.spans] == [
        "classify",
        "chunk",
        "embed",
        "write_edges",
        "link",
    ]
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


def test_housekeeping_prunes_old_traces() -> None:
    from datetime import UTC, datetime, timedelta

    from albert.worker import housekeeping

    with SessionLocal() as session:
        old = Trace(
            kind="request",
            name="old",
            status="ok",
            started_at=datetime.now(UTC) - timedelta(days=30),
            duration_ms=1,
            summary={},
            spans=[],
        )
        fresh = Trace(
            kind="request",
            name="fresh",
            status="ok",
            started_at=datetime.now(UTC),
            duration_ms=1,
            summary={},
            spans=[],
        )
        session.add_all([old, fresh])
        session.commit()
        old_id, fresh_id = old.id, fresh.id
        housekeeping(session)
    # Bulk deletes bypass the identity map; verify through a fresh session.
    with SessionLocal() as session:
        assert session.get(Trace, old_id) is None
        assert session.get(Trace, fresh_id) is not None


async def test_recorder_write_failure_never_fails_the_request(
    client: httpx.AsyncClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from albert import tracing

    class BrokenWriter:
        def submit(self, row: dict) -> bool:
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(tracing, "get_trace_writer", lambda: BrokenWriter())
    response = await client.post("/v1/search", json={"query": "still works"})
    assert response.status_code == 200


async def test_http_exception_span_records_status_not_message() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as anon:
        await anon.post(
            "/v1/search",
            json={"query": "x"},
            headers={"Authorization": "Bearer alb_nope_secret-content-marker-xyz"},
        )
    trace = _latest("POST /v1/search")
    auth = trace.spans[0]
    assert auth["status"] == "error"
    assert auth["detail"]["error"] == "HTTPException"
    assert auth["detail"]["status_code"] == 401
    assert "secret-content-marker" not in json.dumps(trace.spans)


async def test_lifespan_starts_and_stops_the_trace_writer() -> None:
    from albert.trace_writer import get_trace_writer

    writer = get_trace_writer()
    async with app.router.lifespan_context(app):
        assert writer.running
    assert not writer.running
    writer.start()
    assert writer.running

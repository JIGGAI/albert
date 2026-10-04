from __future__ import annotations

import httpx
import pytest

from albert.api import app
from albert.db import SessionLocal
from albert.trace_writer import get_trace_writer

from .conftest import create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio


def _console_client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    )


@pytest.fixture()
def console_key(identity):  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    return create_principal_key(organization, None, "Console", ["console.read", "memory.read"])


async def test_console_requires_capability(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/console/traces")).status_code == 403


async def test_trace_listing_filters_and_paginates(
    client: httpx.AsyncClient, console_key: str
) -> None:
    for i in range(3):
        await client.post("/v1/search", json={"query": f"page probe {i}"})
    get_trace_writer().flush()
    async with _console_client(console_key) as console:
        page = await console.get(
            "/v1/console/traces", params={"name": "POST /v1/search", "limit": 2}
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert len(body["items"]) == 2 and body["next_cursor"]
        assert "spans" not in body["items"][0]
        rest = await console.get(
            "/v1/console/traces",
            params={"name": "POST /v1/search", "limit": 2, "cursor": body["next_cursor"]},
        )
        assert rest.json()["items"][0]["id"] not in {i["id"] for i in body["items"]}
        detail = await console.get(f"/v1/console/traces/{body['items'][0]['id']}")
        assert detail.status_code == 200
        assert [s["name"] for s in detail.json()["spans"]][:2] == ["auth", "resolve_scope"]
        searched = await console.get("/v1/console/traces", params={"q": "page probe 1"})
        assert any(i["summary"]["query"] == "page probe 1" for i in searched.json()["items"])


async def test_graph_snapshot_is_capped(
    client: httpx.AsyncClient, console_key: str, identity
) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    await client.post("/v1/memories", json={"content": "Snapshot Service uses Snapshot Store."})
    drain_jobs()
    org = str(organization.id)
    async with _console_client(console_key) as console:
        too_big = await console.get(
            "/v1/console/graph", params={"organization_id": org, "limit": 5001}
        )
        assert too_big.status_code == 422
        snapshot = await console.get(
            "/v1/console/graph", params={"organization_id": org, "limit": 1}
        )
        body = snapshot.json()
        assert body["truncated"] is True
        assert len(body["nodes"]) == 1
        full = await console.get("/v1/console/graph", params={"organization_id": org})
        nodes = full.json()["nodes"]
        assert {"entity", "memory"} <= {n["kind"] for n in nodes}
        memory_labels = [n["label"] for n in nodes if n["kind"] == "memory"]
        assert all("Snapshot Service uses" not in label for label in memory_labels)
        assert any(e["relation_type"] == "uses" for e in full.json()["edges"])
        assert any(e["relation_type"] == "derived_from" for e in full.json()["edges"])


async def test_overview_reports_jobs_and_traces(
    client: httpx.AsyncClient, console_key: str
) -> None:
    await client.post("/v1/search", json={"query": "overview probe"})
    get_trace_writer().flush()
    async with _console_client(console_key) as console:
        overview = await console.get("/v1/console/overview")
        body = overview.json()
        assert len(body["traces_per_minute"]) == 60
        assert body["trace_count"] >= 1
        assert isinstance(body["jobs"], dict)
        assert "traces_dropped" in body


async def test_operator_memory_detail_reads_full_content(
    client: httpx.AsyncClient, console_key: str
) -> None:
    created = await client.post("/v1/memories", json={"subject": "Detail", "content": "d" * 2000})
    async with _console_client(console_key) as console:
        detail = await console.get(f"/v1/console/memories/{created.json()['id']}")
        assert detail.status_code == 200
        # The reading pane shows a memory in full (capped at 50,000 characters).
        assert len(detail.json()["content"]) == 2000


async def test_stream_endpoint_emits_sse(identity) -> None:  # type: ignore[no-untyped-def]
    """Drive the endpoint's generator directly: the in-memory ASGI transport
    buffers whole responses, so an endless event stream can never complete there."""
    import json
    from datetime import UTC, datetime
    from typing import ClassVar

    from albert.console import stream
    from albert.models import Trace
    from albert.security import AuthContext

    _key, organization, _workspace = identity
    with SessionLocal() as session:
        trace = Trace(
            kind="request",
            name="POST /v1/search",
            organization_id=organization.id,
            status="ok",
            started_at=datetime.now(UTC),
            duration_ms=1,
            summary={"query": "stream probe"},
            spans=[],
        )
        session.add(trace)
        session.commit()
        trace_id = str(trace.id)

    epoch = "1970-01-01T00:00:00+00:00|00000000-0000-0000-0000-000000000000"

    class FakeRequest:
        headers: ClassVar[dict[str, str]] = {"last-event-id": epoch}

    auth = AuthContext(
        principal_id=organization.id,
        organization_id=organization.id,
        workspace_id=None,
        capabilities=frozenset({"console.read"}),
    )

    class FakeSession:
        def close(self) -> None:
            return None

    response = stream(FakeRequest(), auth, FakeSession())  # type: ignore[arg-type]
    assert response.media_type == "text/event-stream"
    body = response.body_iterator
    first = ""
    async for chunk in body:
        if not str(chunk).startswith(":"):
            first = str(chunk)
            break
    await body.aclose()
    assert first.startswith("id: ")
    assert "\nevent: trace\ndata: {" in first
    payload = json.loads(first.split("data: ", 1)[1].strip())
    # From the epoch cursor the first event is the oldest trace, so only the
    # envelope is asserted; the feed tests cover cursor semantics.
    assert {"id", "name", "status", "started_at", "summary"} <= set(payload)
    assert trace_id  # created above so the table is never empty here


async def test_entity_detail_is_audited_and_memory_detail_omits_metadata(
    client: httpx.AsyncClient, console_key: str
) -> None:
    from sqlalchemy import select

    from albert.models import AuditEvent

    created = await client.post(
        "/v1/memories",
        json={"content": "Audited Service uses Audited Store.", "metadata": {"secret_ish": 1}},
    )
    drain_jobs()
    relationship = (await client.post("/v1/graph/query", json={"query": "Audited"})).json()[0]
    async with _console_client(console_key) as console:
        entity = await console.get(f"/v1/console/entities/{relationship['source_entity_id']}")
        assert entity.status_code == 200
        memory = await console.get(f"/v1/console/memories/{created.json()['id']}")
        assert memory.status_code == 200
        assert "metadata" not in memory.json()
    with SessionLocal() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "entity.read",
                AuditEvent.resource_id == relationship["source_entity_id"],
            )
        ).all()
    assert events


def test_graph_cache_is_bounded() -> None:
    from albert import console

    console._graph_cache.clear()
    for i in range(console.GRAPH_CACHE_ENTRIES + 20):
        console._cache_put(f"key-{i}", {"nodes": [], "edges": [], "truncated": False})
    assert len(console._graph_cache) <= console.GRAPH_CACHE_ENTRIES
    assert "key-0" not in console._graph_cache


async def test_stream_releases_its_database_session(identity) -> None:  # type: ignore[no-untyped-def]
    from typing import ClassVar

    from albert.console import stream
    from albert.security import AuthContext

    _key, organization, _workspace = identity
    closed: list[bool] = []

    class FakeSession:
        def close(self) -> None:
            closed.append(True)

    class FakeRequest:
        headers: ClassVar[dict[str, str]] = {}

    auth = AuthContext(
        principal_id=organization.id,
        organization_id=organization.id,
        workspace_id=None,
        capabilities=frozenset({"console.read"}),
    )
    response = stream(FakeRequest(), auth, FakeSession())  # type: ignore[arg-type]
    await response.body_iterator.aclose()
    assert closed == [True]


async def test_organizations_lists_names_and_counts(
    client: httpx.AsyncClient, console_key: str, identity
) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    await client.post("/v1/memories", json={"content": "Counted Service uses Counted Store."})
    drain_jobs()
    async with _console_client(console_key) as console:
        response = await console.get("/v1/console/organizations")
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["items"]}
    mine = rows[str(organization.id)]
    assert mine["name"] == organization.name
    assert mine["memories"] >= 1
    assert mine["entities"] >= 2
    assert mine["relationships"] >= 1
    expected = {"id", "name", "memories", "entities", "relationships"}
    assert all(expected <= set(row) for row in rows.values())


async def test_organizations_requires_console_capability(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/console/organizations")).status_code == 403


async def test_graph_snapshot_is_not_served_stale_after_new_data(console_key: str) -> None:
    """A tenant viewed while empty must show its first memory at once, not after the cache TTL."""
    from .conftest import create_identity

    key, organization, _workspace = create_identity("Fresh Tenant")
    org = str(organization.id)
    async with _console_client(console_key) as console, _console_client(key) as tenant:
        empty = await console.get("/v1/console/graph", params={"organization_id": org})
        assert empty.json()["nodes"] == []
        created = await tenant.post(
            "/v1/memories", json={"content": "Fresh Service uses Fresh Store."}
        )
        assert created.status_code == 201
        drain_jobs()
        after = await console.get("/v1/console/graph", params={"organization_id": org})
    kinds = {node["kind"] for node in after.json()["nodes"]}
    assert {"memory", "entity"} <= kinds

from __future__ import annotations

import httpx
import pytest

from albert.api import app
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


async def test_operator_detail_reads_truncate_content(
    client: httpx.AsyncClient, console_key: str
) -> None:
    created = await client.post("/v1/memories", json={"subject": "Detail", "content": "d" * 2000})
    async with _console_client(console_key) as console:
        detail = await console.get(f"/v1/console/memories/{created.json()['id']}")
        assert detail.status_code == 200
        assert len(detail.json()["content"]) == 500

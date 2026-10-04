from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from albert.api import app
from albert.console_ops import percentile
from albert.db import SessionLocal
from albert.links import process_recall_traces
from albert.models import Job, Trace
from albert.trace_writer import get_trace_writer

from .conftest import create_identity, create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio
VOICE = "Brand voice posts sound sharp confident masculine friendly persuasive energetic bold"


def _client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    )


@pytest.fixture()
async def activity():  # type: ignore[no-untyped-def]
    """A tenant with stores, searches (one finding nothing) and one failed request."""
    key, organization, workspace = create_identity("Ops Agent", workspace_scoped=False)
    console_key = create_principal_key(organization, None, "Console", ["console.read"])
    ids = []
    async with _client(key) as agent:
        for index in range(3):
            created = await agent.post(
                "/v1/memories",
                json={"subject": f"Brand voice {index}", "content": f"{VOICE} v{index}"},
            )
            ids.append(created.json()["id"])
        await agent.post(
            "/v1/memories", json={"subject": "Brand voice 0", "content": f"{VOICE} v0"}
        )
        drain_jobs()
        for _ in range(2):
            found = await agent.post("/v1/search", json={"query": "brand voice sharp confident"})
            assert found.json()["hits"]
        empty = await agent.post(
            "/v1/search", json={"query": "zzzz qqqq xxxx", "include_graph": False}
        )
        assert empty.json()["hits"] == []
        assert (await agent.get(f"/v1/memories/{uuid4()}")).status_code == 404
    get_trace_writer().flush()
    with SessionLocal() as session:
        process_recall_traces(session, settle_seconds=0)
    return console_key, organization, workspace, ids


def test_percentile() -> None:
    assert percentile([], 95) is None
    assert percentile([5.0], 50) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([float(i) for i in range(1, 101)], 95) == pytest.approx(95.05)


async def test_agents_view_counts_activity_per_principal(activity) -> None:  # type: ignore[no-untyped-def]
    console_key, organization, _workspace, _ids = activity
    async with _client(console_key) as console:
        response = await console.get(
            "/v1/console/ops/agents", params={"organization_id": str(organization.id)}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    agent = next(p for p in body["principals"] if p["name"] == "Ops Agent")
    assert agent["searches"] == 3
    assert agent["stores"] == 4
    assert agent["zero_hit_searches"] == 1
    assert agent["errors"] == 1
    assert agent["avg_hits"] > 0 and agent["last_seen"]
    assert agent["type"] == "agent"
    assert sum(bucket["recalls"] for bucket in body["series"]) == 3
    assert sum(bucket["stores"] for bucket in body["series"]) == 4
    assert len(body["series"]) == 24 and body["window_hours"] == 24


async def test_memory_view_reports_health(activity) -> None:  # type: ignore[no-untyped-def]
    console_key, organization, _workspace, ids = activity
    async with _client(console_key) as console:
        response = await console.get(
            "/v1/console/ops/memory", params={"organization_id": str(organization.id)}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert sum(item["count"] for item in body["by_type"]) == 4
    assert body["by_sensitivity"] == [{"label": "internal", "count": 4}]
    assert body["by_workspace"][0]["count"] == 4
    assert body["unindexed"] == 0
    assert body["never_recalled"] <= 1
    assert body["unlinked"] == 0
    assert sum(day["count"] for day in body["added"]) == 4 and len(body["added"]) == 30
    top = body["top_recalled"][0]
    assert top["id"] in ids or top["title"].startswith("Brand voice")
    assert top["recalls"] >= 2 and top["title"].startswith("Brand voice")
    pair = body["near_duplicates"][0]
    assert pair["similarity"] >= 0.97
    assert {pair["a"]["title"], pair["b"]["title"]} == {"Brand voice 0"}


async def test_service_view_reports_endpoints_and_jobs(activity) -> None:  # type: ignore[no-untyped-def]
    console_key, organization, _workspace, _ids = activity
    with SessionLocal() as session:
        session.add(
            Job(
                organization_id=organization.id,
                job_type="enrich_memory",
                payload={"memory_id": str(uuid4())},
                status="failed",
                attempts=5,
                last_error="ValueError: something with 'memory text' inside",
            )
        )
        session.commit()
    async with _client(console_key) as console:
        response = await console.get("/v1/console/ops/service")
    assert response.status_code == 200, response.text
    body = response.json()
    search = next(e for e in body["endpoints"] if e["name"] == "POST /v1/search")
    assert search["count"] >= 3 and search["p50_ms"] > 0 and search["p95_ms"] >= search["p50_ms"]
    read = next(e for e in body["endpoints"] if e["name"] == "GET /v1/memories/{memory_id}")
    assert read["errors"] >= 1
    assert body["totals"]["requests"] >= 8 and 0 < body["totals"]["error_rate"] < 1
    assert body["jobs"]["by_status"]["failed"] >= 1
    failure = body["jobs"]["recent_failures"][0]
    # The message can embed memory text; only the error type leaves the server.
    assert failure["error"] == "ValueError" and failure["job_type"] == "enrich_memory"
    assert body["config"]["embedding_provider"] == "hashing"
    assert body["config"]["graph_store"] == "postgres"
    assert body["traces"]["dropped"] >= 0


async def test_retrieval_view_reports_quality(activity) -> None:  # type: ignore[no-untyped-def]
    console_key, organization, _workspace, _ids = activity
    async with _client(console_key) as console:
        response = await console.get(
            "/v1/console/ops/retrieval", params={"organization_id": str(organization.id)}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["searches"] == 3 and body["zero_hit"] == 1
    assert body["zero_hit_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert body["avg_hits"] > 0 and body["p95_ms"] >= body["p50_ms"] > 0
    backends = {backend["name"]: backend for backend in body["backends"]}
    assert set(backends) == {"lexical", "vector", "graph"}
    assert backends["lexical"]["hit_share"] > 0 and backends["lexical"]["p50_ms"] is not None
    assert 0 <= backends["vector"]["solo_share"] <= 1
    assert body["zero_hit_queries"][0]["query"] == "zzzz qqqq xxxx"
    assert body["zero_hit_queries"][0]["count"] == 1
    assert body["slowest"][0]["trace_id"] and body["slowest"][0]["duration_ms"] > 0


async def test_window_excludes_older_traces(activity) -> None:  # type: ignore[no-untyped-def]
    console_key, organization, _workspace, _ids = activity
    with SessionLocal() as session:
        session.add(
            Trace(
                kind="request",
                name="POST /v1/search",
                organization_id=organization.id,
                status="ok",
                http_status=200,
                started_at=datetime.now(UTC) - timedelta(hours=30),
                duration_ms=5,
                summary={"query": "old", "hits": 2},
                spans=[],
            )
        )
        session.commit()
    async with _client(console_key) as console:
        params = {"organization_id": str(organization.id)}
        day = (await console.get("/v1/console/ops/retrieval", params=params)).json()
        week = (
            await console.get("/v1/console/ops/retrieval", params={**params, "hours": 168})
        ).json()
        too_long = await console.get("/v1/console/ops/retrieval", params={"hours": 5000})
    assert day["searches"] == 3 and week["searches"] == 4
    assert too_long.status_code == 422


async def test_ops_views_require_console_capability(client: httpx.AsyncClient, identity) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    for view in ("agents", "memory", "service", "retrieval"):
        response = await client.get(
            f"/v1/console/ops/{view}", params={"organization_id": str(organization.id)}
        )
        assert response.status_code == 403, view

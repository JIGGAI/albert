from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from albert.api import app
from albert.config import Settings
from albert.trace_writer import get_trace_writer

from .conftest import create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio


async def test_backends_page_reports_stores_builders_and_activity(
    client: httpx.AsyncClient, identity
) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    console_key = create_principal_key(organization, None, "Console", ["console.read"])
    await client.post("/v1/memories", json={"content": "Beacon Service uses Beacon Store."})
    drain_jobs()
    await client.post("/v1/search", json={"query": "Beacon Store"})
    get_trace_writer().flush()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {console_key}"},
    ) as console:
        response = await console.get("/v1/console/backends")
    assert response.status_code == 200, response.text
    body = response.json()

    stores = {store["id"]: store for store in body["stores"]}
    assert list(stores) == ["postgres", "falkordb", "neo4j"]
    postgres = stores["postgres"]
    assert postgres["active"] is True and postgres["available"] is True
    assert postgres["status"]["reachable"] is True and postgres["status"]["version"]
    assert postgres["status"]["latency_ms"] >= 0 and postgres["status"]["error"] is None
    assert postgres["counts"]["entities"] >= 2 and postgres["counts"]["edges"] >= 1
    for name in ("falkordb", "neo4j"):
        other = stores[name]
        assert other["active"] is False and other["available"] is True
        assert other["status"] is None and other["counts"] is None
        assert any(f"ALBERT_GRAPH_STORE={name}" in step for step in other["enable"])
        assert any(f"--profile {name}" in step for step in other["enable"])
        assert other["summary"] and other["choose_when"]

    builders = {builder["id"]: builder for builder in body["builders"]}
    assert list(builders) == ["regex", "llm-classifier", "graphiti"]
    assert builders["regex"]["active"] is True
    assert builders["llm-classifier"]["active"] is False
    assert any("ALBERT_LLM_PROVIDER" in step for step in builders["llm-classifier"]["enable"])
    assert builders["graphiti"]["available"] is False and builders["graphiti"]["active"] is False

    assert body["vectors"]["provider"] == "hashing" and body["vectors"]["chunks"] >= 1
    assert body["vectors"]["memories_embedded"] <= body["vectors"]["memories_total"]

    activity = body["activity"]
    assert activity["indexing_jobs"] >= 1 and activity["edges_written"] >= 1
    assert activity["graph_queries"] >= 1 and activity["graph_failures"] == 0
    assert activity["write_p50_ms"] is not None and activity["classify_p50_ms"] is not None
    assert len(activity["series"]) == 24
    assert sum(bucket["edges_written"] for bucket in activity["series"]) >= 1
    # Connection settings may hold credentials; none of them leave the server.
    assert "redis://" not in response.text and "bolt://" not in response.text


async def test_backends_page_requires_console_capability(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/console/backends")).status_code == 403


def test_neo4j_needs_a_password() -> None:
    with pytest.raises(ValidationError, match="ALBERT_NEO4J_PASSWORD"):
        Settings(graph_store="neo4j", neo4j_password=None)
    assert Settings(graph_store="neo4j", neo4j_password="secret-value").graph_store == "neo4j"

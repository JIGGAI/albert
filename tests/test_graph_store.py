from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from albert.api import app
from albert.config import Settings
from albert.graph import PostgresGraphStore
from albert.graph_store import GRAPH_STORES, get_graph_store, set_graph_store

from .conftest import create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio
SOURCE = Path(__file__).resolve().parents[1] / "src" / "albert"


class RecordingStore:
    """The PostgreSQL store, noting which interface methods the application calls."""

    def __init__(self) -> None:
        self._inner = PostgresGraphStore()
        self.name = self._inner.name
        self.calls: list[str] = []

    def __getattr__(self, item: str):  # type: ignore[no-untyped-def]
        target = getattr(self._inner, item)
        if not callable(target):
            return target

        def recorded(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(item)
            return target(*args, **kwargs)

        return recorded


def test_default_store_is_postgres_and_is_described() -> None:
    assert Settings().graph_store == "postgres"
    assert get_graph_store().name == "postgres"
    assert set(GRAPH_STORES) >= {"postgres", "falkordb", "neo4j"}
    assert GRAPH_STORES["postgres"].available is True
    assert GRAPH_STORES["falkordb"].available is True
    assert Settings(graph_store="falkordb").graph_store == "falkordb"
    for option in GRAPH_STORES.values():
        assert option.summary and option.choose_when


@pytest.mark.parametrize("planned", ["neo4j"])
def test_a_planned_store_is_refused_with_an_explanation(planned: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(graph_store=planned)  # type: ignore[arg-type]
    assert "not available in this build" in str(error.value)
    assert "GRAPH_BACKENDS.md" in str(error.value)


def test_an_unknown_store_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(graph_store="oracle")  # type: ignore[arg-type]


async def test_health_reports_the_graph_store_and_builder(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/health/live")).json()
    assert body["graph_store"] == "postgres"
    assert body["graph_builder"] == "regex"


async def test_every_graph_operation_goes_through_the_store(
    client: httpx.AsyncClient, identity
) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    console_key = create_principal_key(organization, None, "Console", ["console.read"])
    store = RecordingStore()
    set_graph_store(store)  # type: ignore[arg-type]
    try:
        created = await client.post(
            "/v1/memories", json={"content": "Routing Service uses Routing Store."}
        )
        memory_id = created.json()["id"]
        drain_jobs()
        edges = (await client.post("/v1/graph/query", json={"query": "Routing"})).json()
        assert edges and edges[0]["target_name"] == "Routing Store"
        subgraph = await client.get(f"/v1/graph/entities/{edges[0]['source_entity_id']}/subgraph")
        assert subgraph.status_code == 200 and subgraph.json()
        explicit = await client.post(
            "/v1/relationships",
            json={
                "source": {"name": "Routing Team", "entity_type": "team"},
                "relation_type": "owns",
                "target": {"name": "Routing Service", "entity_type": "service"},
            },
        )
        assert explicit.status_code == 201, explicit.text
        assert explicit.json()["source_name"] == "Routing Team"
        search = await client.post("/v1/search", json={"query": "Routing Store"})
        assert any(hit["kind"] == "relationship" for hit in search.json()["hits"])
        exported = await client.get("/v1/export")
        assert [r["relation_type"] for r in exported.json()["relationships"]] == ["owns"]
        patched = await client.patch(
            f"/v1/memories/{memory_id}", json={"valid_until": "2099-01-01T00:00:00+00:00"}
        )
        assert patched.status_code == 200
        rewritten = await client.patch(
            f"/v1/memories/{memory_id}", json={"content": "Routing Service uses Routing Cache."}
        )
        assert rewritten.status_code == 200
        drain_jobs()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {console_key}"},
        ) as console:
            org = str(organization.id)
            graph = await console.get("/v1/console/graph", params={"organization_id": org})
            assert graph.status_code == 200 and graph.json()["edges"]
            entity = await console.get(f"/v1/console/entities/{edges[0]['source_entity_id']}")
            assert entity.status_code == 200 and entity.json()["name"] == "Routing Service"
            assert (await console.get("/v1/console/organizations")).status_code == 200
        assert (await client.delete(f"/v1/memories/{memory_id}")).status_code == 204
    finally:
        set_graph_store(None)
    assert set(store.calls) >= {
        "add_relationship",
        "close_memory_edges",
        "retarget_memory_edges",
        "query",
        "get_entity",
        "subgraph",
        "export_relationships",
        "snapshot",
        "counts",
        "fingerprint",
    }


def test_only_the_postgres_store_touches_the_graph_tables() -> None:
    """Anything else reaching for these tables would silently bypass a configured store."""
    offenders = []
    for path in sorted(SOURCE.glob("*.py")):
        if path.name in {"graph.py", "models.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "albert.models":
                names = {alias.name for alias in node.names}
                if names & {"Entity", "Relationship"}:
                    offenders.append(path.name)
    assert offenders == []

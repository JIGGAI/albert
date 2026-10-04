"""One behavioural contract, run against every graph store that is reachable.

PostgreSQL always runs. FalkorDB and Neo4j run when ALBERT_TEST_FALKORDB_URL or
ALBERT_TEST_NEO4J_URL point at a server (CI starts both); otherwise that
parameter is skipped.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from albert.classifier import ExtractedEntity, ExtractedRelationship
from albert.db import SessionLocal
from albert.graph import PostgresGraphStore
from albert.models import Memory

FALKORDB_URL = os.environ.get("ALBERT_TEST_FALKORDB_URL")
NEO4J_URL = os.environ.get("ALBERT_TEST_NEO4J_URL")
NEO4J_PASSWORD = os.environ.get("ALBERT_TEST_NEO4J_PASSWORD", "")
ALL = ["public", "internal", "confidential", "restricted"]


@pytest.fixture(scope="module")
def neo4j_store():  # type: ignore[no-untyped-def]
    from albert.graph_cypher import Neo4jGraphStore

    return Neo4jGraphStore(NEO4J_URL, user="neo4j", password=NEO4J_PASSWORD)


@pytest.fixture(params=["postgres", "falkordb", "neo4j"])
def store(request):  # type: ignore[no-untyped-def]
    if request.param == "postgres":
        return PostgresGraphStore()
    if request.param == "neo4j":
        if not NEO4J_URL:
            pytest.skip("ALBERT_TEST_NEO4J_URL is not set")
        # Tests share one database; every test works in organizations of its own.
        return request.getfixturevalue("neo4j_store")
    if not FALKORDB_URL:
        pytest.skip("ALBERT_TEST_FALKORDB_URL is not set")
    from albert.graph_cypher import FalkorDBGraphStore

    return FalkorDBGraphStore(FALKORDB_URL, graph_name=f"contract_{uuid4().hex}")


def test_ping_reports_a_version(store, session) -> None:  # type: ignore[no-untyped-def]
    assert store.ping(session) not in ("", "unknown")


@pytest.fixture()
def session():  # type: ignore[no-untyped-def]
    with SessionLocal() as db:
        yield db
        db.rollback()


def _edge(
    source: str, relation: str, target: str, confidence: float = 0.8
) -> ExtractedRelationship:
    return ExtractedRelationship(
        ExtractedEntity(source, "service", f"{source} description", 0.9),
        relation,
        ExtractedEntity(target, "service"),
        confidence,
    )


def _memory(session, org):  # type: ignore[no-untyped-def]
    """A real memory row: the PostgreSQL store's edges reference memories."""
    memory = Memory(organization_id=org, owner_principal_id=uuid4(), content="contract fixture")
    session.add(memory)
    session.flush()
    return memory.id


def _add(store, session, org, ws, edge, **extra):  # type: ignore[no-untyped-def]
    values = {
        "source_memory_id": None,
        "sensitivity": "internal",
        "valid_from": datetime.now(UTC) - timedelta(hours=1),
        **extra,
    }
    return store.add_relationship(
        session, organization_id=org, workspace_id=ws, extracted=edge, **values
    )


def _query(store, session, org, text, **extra):  # type: ignore[no-untyped-def]
    values = {"workspace_id": None, "temporal_as_of": None, "limit": 20, "sensitivities": ALL}
    return store.query(session, organization_id=org, query=text, **{**values, **extra})


def test_add_is_idempotent_and_returns_both_entities(store, session) -> None:  # type: ignore[no-untyped-def]
    org, ws = uuid4(), uuid4()
    first, created = _add(store, session, org, ws, _edge("Billing API", "uses", "Ledger DB"))
    again, created_again = _add(
        store, session, org, ws, _edge("billing  api", "uses", "Ledger DB", 0.95)
    )
    assert created is True and created_again is False
    assert again.id == first.id
    assert again.source.id == first.source.id and again.source.name == "Billing API"
    assert again.confidence == pytest.approx(0.95)
    assert first.source.description == "Billing API description"
    assert first.read().target_name == "Ledger DB"
    assert first.organization_id == org and first.workspace_id == ws


def test_query_matches_words_and_respects_scope(store, session) -> None:  # type: ignore[no-untyped-def]
    org, other_org, ws, other_ws = uuid4(), uuid4(), uuid4(), uuid4()
    _add(store, session, org, ws, _edge("Billing API", "uses", "Ledger DB", 0.9))
    _add(store, session, org, other_ws, _edge("Billing Worker", "depends_on", "Queue", 0.5))
    _add(store, session, other_org, uuid4(), _edge("Billing API", "uses", "Other Ledger"))
    _add(
        store,
        session,
        org,
        ws,
        _edge("Vault", "stores", "Billing Secrets"),
        sensitivity="restricted",
    )

    everything = _query(store, session, org, "billing")
    assert [r.target_name for r in everything] == ["Ledger DB", "Billing Secrets", "Queue"] or {
        r.target_name for r in everything
    } == {"Ledger DB", "Billing Secrets", "Queue"}
    assert everything[0].confidence >= everything[-1].confidence
    assert {r.target_name for r in _query(store, session, org, "billing", workspace_id=ws)} == {
        "Ledger DB",
        "Billing Secrets",
    }
    visible = _query(store, session, org, "billing", sensitivities=["public", "internal"])
    assert "Billing Secrets" not in {r.target_name for r in visible}
    assert [r.target_name for r in _query(store, session, org, "DEPENDS_ON")] == ["Queue"]
    assert _query(store, session, org, ".") == []
    assert _query(store, session, org, "nothing-matches-this") == []
    assert len(_query(store, session, org, "billing", limit=1)) == 1


def test_validity_closing_and_time_travel(store, session) -> None:  # type: ignore[no-untyped-def]
    org, ws = uuid4(), uuid4()
    memory, other_memory = _memory(session, org), _memory(session, org)
    _add(
        store,
        session,
        org,
        ws,
        _edge("Router", "uses", "Cache"),
        source_memory_id=memory,
        metadata={"extracted": True},
    )
    _add(store, session, org, ws, _edge("Router", "owned_by", "Platform"), source_memory_id=memory)
    _add(
        store,
        session,
        org,
        ws,
        _edge("Router", "uses", "Queue"),
        source_memory_id=other_memory,
        metadata={"extracted": True},
    )
    before = datetime.now(UTC)
    at = before + timedelta(seconds=1)

    closed = store.close_memory_edges(
        session, organization_id=org, memory_id=memory, at=at, extracted_only=True
    )
    assert closed == 1
    assert (
        store.close_memory_edges(
            session, organization_id=org, memory_id=memory, at=at, extracted_only=True
        )
        == 0
    )
    later = at + timedelta(seconds=1)
    now_names = {r.target_name for r in _query(store, session, org, "router", temporal_as_of=later)}
    assert now_names == {"Platform", "Queue"}
    then_names = {
        r.target_name for r in _query(store, session, org, "router", temporal_as_of=before)
    }
    assert then_names == {"Cache", "Platform", "Queue"}

    # Re-adding a closed fact opens a new edge rather than reviving the old one.
    _reopened, created = _add(
        store,
        session,
        org,
        ws,
        _edge("Router", "uses", "Cache"),
        source_memory_id=memory,
        metadata={"extracted": True},
    )
    assert created is True
    assert (
        store.close_memory_edges(
            session, organization_id=org, memory_id=memory, at=later, extracted_only=False
        )
        == 2
    )


def test_retarget_moves_only_extracted_edges(store, session) -> None:  # type: ignore[no-untyped-def]
    org, ws = uuid4(), uuid4()
    memory = _memory(session, org)
    _add(
        store,
        session,
        org,
        ws,
        _edge("Gateway", "uses", "Auth"),
        source_memory_id=memory,
        metadata={"extracted": True},
    )
    _add(
        store, session, org, ws, _edge("Gateway", "owned_by", "Edge Team"), source_memory_id=memory
    )
    moved = store.retarget_memory_edges(
        session, organization_id=org, memory_id=memory, sensitivity="confidential", valid_until=None
    )
    assert moved == 1
    visible = {
        r.target_name for r in _query(store, session, org, "gateway", sensitivities=["internal"])
    }
    assert visible == {"Edge Team"}


def test_entities_and_subgraph(store, session) -> None:  # type: ignore[no-untyped-def]
    org, ws = uuid4(), uuid4()
    first, _ = _add(store, session, org, ws, _edge("A Service", "uses", "B Service"))
    _add(store, session, org, ws, _edge("B Service", "uses", "C Service"))
    _add(store, session, org, ws, _edge("C Service", "uses", "D Service"))
    entity = store.get_entity(session, entity_id=first.source.id, organization_id=org)
    assert entity is not None and entity.name == "A Service" and entity.workspace_id == ws
    assert store.get_entity(session, entity_id=first.source.id) is not None
    assert store.get_entity(session, entity_id=first.source.id, organization_id=uuid4()) is None
    assert store.get_entity(session, entity_id=uuid4()) is None

    def reach(hops: int) -> set[str]:
        rows = store.subgraph(
            session,
            organization_id=org,
            workspace_id=ws,
            entity_id=first.source.id,
            hops=hops,
            temporal_as_of=None,
            limit=50,
            sensitivities=ALL,
        )
        return {r.target_name for r in rows}

    assert reach(1) == {"B Service"}
    assert reach(2) == {"B Service", "C Service"}
    assert reach(3) == {"B Service", "C Service", "D Service"}


def test_export_snapshot_counts_and_fingerprint(store, session) -> None:  # type: ignore[no-untyped-def]
    org, ws = uuid4(), uuid4()
    empty = store.fingerprint(session, organization_id=org)
    _add(
        store, session, org, ws, _edge("Planner", "uses", "Calendar"), metadata={"extracted": True}
    )
    _add(
        store, session, org, ws, _edge("Planner", "owned_by", "Ops Team"), metadata={"note": "kept"}
    )
    assert store.fingerprint(session, organization_id=org) != empty

    exported = store.export_relationships(
        session, organization_id=org, workspace_id=None, sensitivities=ALL
    )
    assert [(r.relation_type, r.metadata.get("note")) for r in exported] == [("owned_by", "kept")]
    assert (
        store.export_relationships(
            session, organization_id=org, workspace_id=uuid4(), sensitivities=ALL
        )
        == []
    )

    now = datetime.now(UTC)
    entities, edges, truncated = store.snapshot(
        session, organization_id=org, workspace_id=None, when=now, limit=10
    )
    assert {e.name for e in entities} == {"Planner", "Calendar", "Ops Team"}
    assert len(edges) == 2 and truncated is False
    capped, capped_edges, was_truncated = store.snapshot(
        session, organization_id=org, workspace_id=None, when=now, limit=1
    )
    assert len(capped) == 1 and was_truncated is True and capped_edges == []
    assert store.counts(session, when=now)[org] == (3, 2)


@pytest.mark.anyio
async def test_the_api_works_end_to_end_on_falkordb(client, identity) -> None:  # type: ignore[no-untyped-def]
    """Indexing, graph query, hybrid search, traversal and forgetting through FalkorDB."""
    if not FALKORDB_URL:
        pytest.skip("ALBERT_TEST_FALKORDB_URL is not set")
    from albert.graph_cypher import FalkorDBGraphStore
    from albert.graph_store import set_graph_store

    from .conftest import drain_jobs

    set_graph_store(FalkorDBGraphStore(FALKORDB_URL, graph_name=f"api_{uuid4().hex}"))
    try:
        health = (await client.get("/v1/health/live")).json()
        assert health["graph_store"] == "falkordb"
        created = await client.post(
            "/v1/memories", json={"content": "Falcon Service uses Falcon Store."}
        )
        memory_id = created.json()["id"]
        drain_jobs()
        edges = (await client.post("/v1/graph/query", json={"query": "Falcon"})).json()
        assert [edge["target_name"] for edge in edges] == ["Falcon Store"]
        assert edges[0]["source_memory_id"] == memory_id
        around = await client.get(f"/v1/graph/entities/{edges[0]['source_entity_id']}/subgraph")
        assert around.status_code == 200 and len(around.json()) == 1
        search = (await client.post("/v1/search", json={"query": "Falcon Store"})).json()
        assert any(hit["kind"] == "relationship" for hit in search["hits"])
        explicit = await client.post(
            "/v1/relationships",
            json={
                "source": {"name": "Falcon Team", "entity_type": "team"},
                "relation_type": "owns",
                "target": {"name": "Falcon Service", "entity_type": "service"},
            },
        )
        assert explicit.status_code == 201, explicit.text
        exported = (await client.get("/v1/export")).json()
        assert [r["relation_type"] for r in exported["relationships"]] == ["owns"]
        assert (await client.delete(f"/v1/memories/{memory_id}")).status_code == 204
        remaining = (await client.post("/v1/graph/query", json={"query": "Falcon"})).json()
        assert [edge["relation_type"] for edge in remaining] == ["owns"]
    finally:
        set_graph_store(None)

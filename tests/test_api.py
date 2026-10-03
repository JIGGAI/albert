from __future__ import annotations

import httpx
import pytest

from albert.api import app
from albert.db import SessionLocal
from albert.worker import claim_job, process_job

from .conftest import create_identity

pytestmark = pytest.mark.anyio


def drain_jobs() -> None:
    while True:
        with SessionLocal() as session:
            job = claim_job(session, "test-worker")
            if job is None:
                return
            process_job(session, job)


async def test_health_and_auth(client: httpx.AsyncClient) -> None:
    health = await client.get("/v1/health/live")
    assert health.status_code == 200
    assert health.json()["embedding_provider"] == "hashing"
    assert health.json()["embedding_dimensions"] == 64
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        response = await anonymous.post("/v1/search", json={"query": "anything"})
        assert response.status_code == 401


async def test_memory_enrichment_and_hybrid_search(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/memories",
        json={
            "subject": "Deployment architecture",
            "content": "Albert uses PostgreSQL and depends on pgvector.",
            "memory_type": "fact",
        },
    )
    assert response.status_code == 201, response.text
    memory_id = response.json()["id"]
    drain_jobs()

    retrieved = await client.get(f"/v1/memories/{memory_id}")
    assert retrieved.status_code == 200
    assert retrieved.json()["embedding_model"].startswith("hashing-v1")

    search = await client.post(
        "/v1/search", json={"query": "PostgreSQL pgvector", "limit": 10}
    )
    assert search.status_code == 200, search.text
    payload = search.json()
    memory_hit = next(hit for hit in payload["hits"] if hit["memory_id"] == memory_id)
    assert "lexical" in memory_hit["backends"]
    assert "vector" in memory_hit["backends"]
    assert payload["degraded"] == []
    context = await client.post(
        "/v1/context/assemble",
        json={"query": "PostgreSQL pgvector", "limit": 10, "max_characters": 2000},
    )
    assert context.status_code == 200
    assert "Deployment architecture" in context.json()["context"]
    assert context.json()["citations"][0]["memory_id"] == memory_id


async def test_episode_is_canonical_and_idempotent(client: httpx.AsyncClient) -> None:
    payload = {
        "content": "The release procedure uses signed containers.",
        "source_type": "test",
        "source_uri": "test://release/1",
    }
    first = await client.post("/v1/episodes", json=payload)
    second = await client.post("/v1/episodes", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    drain_jobs()
    status = await client.get(f"/v1/episodes/{first.json()['id']}")
    assert status.json()["enrichment_status"] == "complete"


async def test_graph_write_query_and_traversal(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/v1/relationships",
        json={
            "source": {"name": "Albert", "entity_type": "project"},
            "relation_type": "uses",
            "target": {"name": "PostgreSQL", "entity_type": "tool"},
        },
    )
    assert created.status_code == 201, created.text
    relationship = created.json()
    query = await client.post("/v1/graph/query", json={"query": "Albert", "limit": 10})
    assert query.status_code == 200
    assert any(row["id"] == relationship["id"] for row in query.json())
    graph = await client.get(
        f"/v1/graph/entities/{relationship['source_entity_id']}/subgraph?hops=2"
    )
    assert graph.status_code == 200
    assert graph.json()[0]["target_name"] == "PostgreSQL"


async def test_reenrichment_expires_stale_extracted_relationships(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/v1/memories",
        json={"subject": "Runtime", "content": "Runtime uses Database Alpha."},
    )
    assert created.status_code == 201
    memory_id = created.json()["id"]
    drain_jobs()
    before = await client.post("/v1/graph/query", json={"query": "Alpha"})
    assert before.status_code == 200
    assert before.json()

    updated = await client.patch(
        f"/v1/memories/{memory_id}",
        json={"content": "Runtime uses Database Beta."},
    )
    assert updated.status_code == 200
    drain_jobs()
    stale = await client.post("/v1/graph/query", json={"query": "Alpha"})
    current = await client.post("/v1/graph/query", json={"query": "Beta"})
    assert stale.json() == []
    assert current.json()


async def test_working_memory_and_fenced_locks(client: httpx.AsyncClient) -> None:
    working = await client.post(
        "/v1/working-memory",
        json={"task_id": "task-lock", "description": "Test lock lifecycle"},
    )
    assert working.status_code == 201
    working_id = working.json()["id"]
    read_working = await client.get(f"/v1/working-memory/{working_id}")
    assert read_working.status_code == 200
    assert read_working.json()["task_id"] == "task-lock"
    acquired = await client.post(
        "/v1/locks/acquire",
        json={
            "working_memory_id": working_id,
            "resource_type": "repository",
            "resource_ref": "JIGGAI/albert",
            "ttl_seconds": 60,
        },
    )
    assert acquired.status_code == 201, acquired.text
    lease = acquired.json()
    conflict = await client.post(
        "/v1/locks/acquire",
        json={
            "resource_type": "repository",
            "resource_ref": "JIGGAI/albert",
            "ttl_seconds": 60,
        },
    )
    assert conflict.status_code == 409
    renewed = await client.post(
        f"/v1/locks/{lease['id']}/renew",
        json={"token": lease["token"], "fence": lease["fence"], "ttl_seconds": 120},
    )
    assert renewed.status_code == 200
    completed = await client.post(
        f"/v1/working-memory/{working_id}/complete",
        json={"consolidation_notes": "Verified"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    reacquired = await client.post(
        "/v1/locks/acquire",
        json={
            "resource_type": "repository",
            "resource_ref": "JIGGAI/albert",
            "ttl_seconds": 60,
        },
    )
    assert reacquired.status_code == 201
    assert reacquired.json()["fence"] == lease["fence"] + 1
    stale = await client.post(
        f"/v1/locks/{lease['id']}/renew",
        json={"token": lease["token"], "fence": lease["fence"], "ttl_seconds": 60},
    )
    assert stale.status_code == 409


async def test_tenant_isolation(client: httpx.AsyncClient) -> None:
    memory = await client.post("/v1/memories", json={"content": "Tenant one private fact"})
    assert memory.status_code == 201
    other_key, _organization, _workspace = create_identity("Other Tenant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_key}"},
    ) as other:
        assert (await other.get(f"/v1/memories/{memory.json()['id']}")).status_code == 404
        result = await other.post("/v1/search", json={"query": "Tenant one private fact"})
        assert result.status_code == 200
        assert result.json()["hits"] == []


async def test_secret_screening(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/memories", json={"content": "api_key = this-is-a-secret-value-123456"}
    )
    assert response.status_code == 422


async def test_sensitivity_is_enforced_for_memory_and_graph(client: httpx.AsyncClient) -> None:
    memory = await client.post(
        "/v1/memories",
        json={
            "content": "Restricted Project uses Restricted Database.",
            "sensitivity": "restricted",
        },
    )
    assert memory.status_code == 201
    drain_jobs()
    assert (await client.get(f"/v1/memories/{memory.json()['id']}")).status_code == 404
    search = await client.post("/v1/search", json={"query": "Restricted Database"})
    assert search.status_code == 200
    assert search.json()["hits"] == []
    forbidden = await client.post(
        "/v1/search", json={"query": "Restricted", "sensitivity": ["restricted"]}
    )
    assert forbidden.status_code == 403
    graph = await client.post("/v1/graph/query", json={"query": "Restricted"})
    assert graph.status_code == 200
    assert graph.json() == []


async def test_delete_invalidates_memory(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/memories", json={"content": "Disposable memory marker"})
    memory_id = created.json()["id"]
    assert (await client.delete(f"/v1/memories/{memory_id}")).status_code == 204
    assert (await client.get(f"/v1/memories/{memory_id}")).status_code == 404
    result = await client.post("/v1/search", json={"query": "Disposable memory marker"})
    assert all(hit["memory_id"] != memory_id for hit in result.json()["hits"])

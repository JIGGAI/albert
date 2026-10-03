from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from albert.api import app
from albert.db import SessionLocal
from albert.models import APIKey

from .conftest import create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio


def _client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    )


async def test_whitespace_only_content_is_rejected(client: httpx.AsyncClient) -> None:
    memory = await client.post("/v1/memories", json={"content": "   \n  "})
    episode = await client.post("/v1/episodes", json={"content": "\t \n"})
    assert memory.status_code == 422
    assert episode.status_code == 422


async def test_episode_dedupe_is_per_principal_and_hides_unreadable_episodes(
    client: httpx.AsyncClient, identity
) -> None:  # type: ignore[no-untyped-def]
    key, organization, workspace = identity
    with SessionLocal() as session:
        record = session.scalar(select(APIKey).where(APIKey.prefix == key.split("_")[1]))
        assert record is not None
        record.capabilities = [*record.capabilities, "memory.restricted"]
        session.commit()
    payload = {"content": "Board approved the Acme acquisition.", "sensitivity": "restricted"}
    first = await client.post("/v1/episodes", json=payload)
    assert first.status_code == 201, first.text
    repeat = await client.post("/v1/episodes", json=payload)
    assert repeat.status_code == 200
    assert repeat.json()["id"] == first.json()["id"]

    other_key = create_principal_key(
        organization, workspace, "Internal Only", ["memory.read", "memory.write"]
    )
    async with _client(other_key) as other:
        response = await other.post(
            "/v1/episodes", json={**payload, "sensitivity": "internal"}
        )
    assert response.status_code == 201, response.text
    assert response.json()["id"] != first.json()["id"]
    assert response.json()["sensitivity"] == "internal"


async def test_non_content_edit_does_not_duplicate_temporal_edges(
    client: httpx.AsyncClient,
) -> None:
    valid_from = datetime.now(UTC) - timedelta(days=2)
    created = await client.post(
        "/v1/memories",
        json={"content": "Runtime uses Database Alpha.", "valid_from": valid_from.isoformat()},
    )
    memory_id = created.json()["id"]
    drain_jobs()
    far_future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    patched = await client.patch(f"/v1/memories/{memory_id}", json={"valid_until": far_future})
    assert patched.status_code == 200, patched.text
    drain_jobs()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    as_of_yesterday = await client.post(
        "/v1/graph/query", json={"query": "Alpha", "temporal_as_of": yesterday}
    )
    now = await client.post("/v1/graph/query", json={"query": "Alpha"})
    assert len(as_of_yesterday.json()) == 1
    assert len(now.json()) == 1
    assert now.json()[0]["valid_until"] is not None


async def test_working_memory_can_be_read_with_read_capability(
    client: httpx.AsyncClient, identity
) -> None:  # type: ignore[no-untyped-def]
    _key, organization, workspace = identity
    created = await client.post(
        "/v1/working-memory", json={"task_id": "readable-task", "description": "Readable"}
    )
    assert created.status_code == 201
    reader_key = create_principal_key(organization, workspace, "Reader", ["working_memory.read"])
    async with _client(reader_key) as reader:
        response = await reader.get(f"/v1/working-memory/{created.json()['id']}")
    assert response.status_code == 200


async def test_subgraph_honors_temporal_as_of(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/v1/relationships",
        json={
            "source": {"name": "Temporal Source", "entity_type": "project"},
            "relation_type": "uses",
            "target": {"name": "Temporal Target", "entity_type": "tool"},
        },
    )
    assert created.status_code == 201
    entity_id = created.json()["source_entity_id"]
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    current = await client.get(f"/v1/graph/entities/{entity_id}/subgraph")
    historical = await client.get(
        f"/v1/graph/entities/{entity_id}/subgraph", params={"temporal_as_of": yesterday}
    )
    assert len(current.json()) == 1
    assert historical.json() == []


async def test_api_key_last_used_is_throttled(client: httpx.AsyncClient, identity) -> None:  # type: ignore[no-untyped-def]
    key, _organization, _workspace = identity
    prefix = key.split("_")[1]
    recent = datetime.now(UTC) - timedelta(seconds=5)
    stale = datetime.now(UTC) - timedelta(minutes=10)
    with SessionLocal() as session:
        record = session.scalar(select(APIKey).where(APIKey.prefix == prefix))
        record.last_used_at = recent
        session.commit()
    assert (await client.get("/v1/health/live")).status_code == 200
    assert (await client.post("/v1/search", json={"query": "anything"})).status_code == 200
    with SessionLocal() as session:
        record = session.scalar(select(APIKey).where(APIKey.prefix == prefix))
        assert record.last_used_at.replace(tzinfo=UTC) == recent
        record.last_used_at = stale
        session.commit()
    assert (await client.post("/v1/search", json={"query": "anything"})).status_code == 200
    with SessionLocal() as session:
        record = session.scalar(select(APIKey).where(APIKey.prefix == prefix))
        assert record.last_used_at.replace(tzinfo=UTC) > stale

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from albert.db import SessionLocal

from .conftest import drain_jobs

pytestmark = pytest.mark.anyio

PARAGRAPHS = [
    "Opening paragraph about release trains, cadence meetings and ticket grooming. " * 8,
    "Second paragraph covering invoice reconciliation, ledgers and quarterly close. " * 8,
    "Third paragraph on telemetry dashboards, alert fatigue and on-call rotations. " * 8,
    "Fourth paragraph about greenhouse irrigation schedules, soil sensors and harvest logs. " * 8,
    "Closing paragraph: the lighthouse keeper catalogued migratory puffins each dawn. " * 8,
]
LONG_CONTENT = "\n\n".join(PARAGRAPHS)


def test_chunker_splits_on_paragraphs_within_budget() -> None:
    from albert.chunking import chunk_text

    chunks = chunk_text(LONG_CONTENT, max_characters=1500, overlap_characters=200)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1500 for chunk in chunks)
    assert "".join(PARAGRAPHS[-1].split()) in "".join(chunks[-1].split())
    assert chunk_text("short note", max_characters=1500, overlap_characters=200) == ["short note"]


def test_chunker_hard_splits_an_unbroken_run() -> None:
    from albert.chunking import chunk_text

    chunks = chunk_text("x" * 4000, max_characters=1500, overlap_characters=100)
    assert len(chunks) == 3
    assert all(len(chunk) <= 1500 for chunk in chunks)


async def test_late_content_is_vector_searchable_through_chunks(
    client: httpx.AsyncClient,
) -> None:
    from albert.models import MemoryChunk

    created = await client.post(
        "/v1/memories", json={"subject": "Long operational notes", "content": LONG_CONTENT}
    )
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]
    drain_jobs()
    with SessionLocal() as session:
        chunks = list(
            session.scalars(select(MemoryChunk).where(MemoryChunk.memory_id == UUID(memory_id)))
        )
    assert len(chunks) > 1
    assert all(chunk.embedding is not None and chunk.embedding_model for chunk in chunks)
    result = await client.post(
        "/v1/search",
        json={"query": PARAGRAPHS[-1].strip(), "include_graph": False, "include_content": False},
    )
    hit = next(h for h in result.json()["hits"] if h["memory_id"] == memory_id)
    assert "vector" in hit["backends"]
    assert hit["metadata"]["chunk_index"] > 0


async def test_reenrichment_replaces_chunks(client: httpx.AsyncClient) -> None:
    from albert.models import MemoryChunk

    created = await client.post("/v1/memories", json={"content": LONG_CONTENT})
    memory_id = created.json()["id"]
    drain_jobs()
    patched = await client.patch(f"/v1/memories/{memory_id}", json={"content": "Now tiny."})
    assert patched.status_code == 200
    drain_jobs()
    with SessionLocal() as session:
        chunks = list(
            session.scalars(select(MemoryChunk).where(MemoryChunk.memory_id == UUID(memory_id)))
        )
    assert [chunk.content for chunk in chunks] == ["Now tiny."]


async def test_delete_removes_chunks(client: httpx.AsyncClient) -> None:
    from albert.models import MemoryChunk

    created = await client.post("/v1/memories", json={"content": LONG_CONTENT})
    memory_id = created.json()["id"]
    drain_jobs()
    assert (await client.delete(f"/v1/memories/{memory_id}")).status_code == 204
    with SessionLocal() as session:
        remaining = session.scalars(
            select(MemoryChunk).where(MemoryChunk.memory_id == UUID(memory_id))
        ).all()
    assert remaining == []


async def test_chunk_count_is_capped_and_recorded(
    client: httpx.AsyncClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from albert import services
    from albert.config import get_settings
    from albert.models import Memory, MemoryChunk

    capped = get_settings().model_copy(update={"max_chunks_per_memory": 2})
    monkeypatch.setattr(services, "get_settings", lambda: capped)
    created = await client.post("/v1/memories", json={"content": LONG_CONTENT})
    memory_id = created.json()["id"]
    drain_jobs()
    with SessionLocal() as session:
        chunks = session.scalars(
            select(MemoryChunk).where(MemoryChunk.memory_id == UUID(memory_id))
        ).all()
        memory = session.get(Memory, UUID(memory_id))
        assert len(chunks) == 2
        assert memory.metadata_["embedding"]["truncated"] is True
        assert memory.metadata_["embedding"]["chunks"] == 2

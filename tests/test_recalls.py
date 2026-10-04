from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from albert.db import SessionLocal
from albert.links import process_recall_traces
from albert.models import MemoryLink, MemoryRecall, MemoryStat, Trace
from albert.trace_writer import get_trace_writer

from .conftest import drain_jobs

pytestmark = pytest.mark.anyio


async def _remember(client: httpx.AsyncClient, content: str) -> str:
    response = await client.post("/v1/memories", json={"content": content})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _process() -> int:
    get_trace_writer().flush()
    with SessionLocal() as session:
        return process_recall_traces(session, settle_seconds=0)


def _latest(name: str) -> Trace:
    get_trace_writer().flush()
    with SessionLocal() as session:
        trace = session.scalar(
            select(Trace).where(Trace.name == name).order_by(Trace.started_at.desc())
        )
        assert trace is not None, name
        return trace


def _stat(memory_id: str) -> MemoryStat | None:
    with SessionLocal() as session:
        return session.get(MemoryStat, UUID(memory_id))


def _search_trace(organization_id: UUID, memory_ids: list[str], name: str) -> None:
    with SessionLocal() as session:
        session.add(
            Trace(
                kind="request",
                name=name,
                organization_id=organization_id,
                status="ok",
                http_status=200,
                duration_ms=1,
                summary={"query": "manual", "memory_ids": memory_ids},
                spans=[],
            )
        )
        session.commit()


async def test_search_trace_summary_lists_memory_ids(client: httpx.AsyncClient) -> None:
    memory_id = await _remember(client, "Clipper guards are sanitized between every haircut.")
    drain_jobs()
    result = await client.post("/v1/search", json={"query": "clipper guards sanitized"})
    assert result.status_code == 200
    trace = _latest("POST /v1/search")
    assert trace.summary["memory_ids"][0] == memory_id
    context = await client.post("/v1/context/assemble", json={"query": "clipper guards sanitized"})
    assert context.status_code == 200
    assert memory_id in _latest("POST /v1/context/assemble").summary["memory_ids"]


async def test_store_trace_summary_lists_stored_ids(client: httpx.AsyncClient) -> None:
    memory_id = await _remember(client, "Walk-in wait times are posted at the front desk.")
    assert _latest("POST /v1/memories").summary["stored_ids"] == [memory_id]
    drain_jobs()
    assert _latest("enrich_memory").summary["stored_ids"] == [memory_id]
    episode = await client.post(
        "/v1/episodes",
        json={
            "source_type": "document",
            "source_uri": f"file:///notes/{uuid4()}.md",
            "content": "Beard trims are booked in fifteen minute slots.",
        },
    )
    assert episode.status_code == 201, episode.text
    drain_jobs()
    stored = _latest("enrich_episode").summary["stored_ids"]
    assert len(stored) == 1 and UUID(stored[0])


async def test_recall_processing_counts_hits_and_pairs(client: httpx.AsyncClient, identity) -> None:  # type: ignore[no-untyped-def]
    first = await _remember(client, "Fade haircut pricing is posted beside every station.")
    second = await _remember(client, "Fade haircut pricing changes are announced on Mondays.")
    drain_jobs()
    _process()  # drain whatever earlier tests left behind
    result = await client.post("/v1/search", json={"query": "fade haircut pricing"})
    hit_ids = [hit["memory_id"] for hit in result.json()["hits"] if hit["memory_id"]]
    assert {first, second} <= set(hit_ids)
    assert _process() == 1
    for memory_id in (first, second):
        stat = _stat(memory_id)
        assert stat is not None and stat.recall_count == 1
        assert stat.last_recalled_at is not None
    with SessionLocal() as session:
        recalls = list(
            session.scalars(select(MemoryRecall).where(MemoryRecall.memory_id == UUID(first)))
        )
        assert len(recalls) == 1
        assert recalls[0].query == "fade haircut pricing"
        assert recalls[0].rank == hit_ids.index(first) + 1
        assert recalls[0].principal_id is not None
        source, target = sorted([first, second])
        link = session.scalar(
            select(MemoryLink).where(
                MemoryLink.kind == "recalled",
                MemoryLink.source_memory_id == UUID(source),
                MemoryLink.target_memory_id == UUID(target),
            )
        )
        assert link is not None and link.count == 1 and link.weight == 1.0
    await client.post("/v1/search", json={"query": "fade haircut pricing"})
    assert _process() == 1
    with SessionLocal() as session:
        link = session.scalar(
            select(MemoryLink).where(
                MemoryLink.kind == "recalled",
                MemoryLink.source_memory_id == UUID(source),
                MemoryLink.target_memory_id == UUID(target),
            )
        )
        assert link is not None and link.count == 2 and link.weight == 2.0
    assert _stat(first).recall_count == 2  # type: ignore[union-attr]


async def test_recall_processing_is_idempotent_across_runs(client: httpx.AsyncClient) -> None:
    memory_id = await _remember(client, "Hot towel shaves use eucalyptus oil.")
    drain_jobs()
    _process()
    await client.post("/v1/search", json={"query": "hot towel shaves eucalyptus"})
    assert _process() == 1
    assert _process() == 0
    assert _process() == 0
    assert _stat(memory_id).recall_count == 1  # type: ignore[union-attr]


async def test_console_searches_are_not_counted(client: httpx.AsyncClient, identity) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    memory_id = await _remember(client, "Pomade stock is counted on Fridays.")
    drain_jobs()
    _process()
    _search_trace(organization.id, [memory_id], "POST /v1/console/search")
    assert _process() == 0
    assert _stat(memory_id) is None
    _search_trace(organization.id, [memory_id, str(uuid4())], "POST /v1/search")
    assert _process() == 1  # a hit that no longer exists is skipped, not fatal
    assert _stat(memory_id).recall_count == 1  # type: ignore[union-attr]


async def test_old_recalls_are_pruned_with_traces(client: httpx.AsyncClient, identity) -> None:  # type: ignore[no-untyped-def]
    from albert.worker import housekeeping

    _key, organization, _workspace = identity
    memory_id = await _remember(client, "Station mirrors are cleaned at close.")
    now = datetime.now(UTC)
    with SessionLocal() as session:
        for age_days in (30, 0):
            session.add(
                MemoryRecall(
                    memory_id=UUID(memory_id),
                    organization_id=organization.id,
                    trace_id=uuid4(),
                    recalled_at=now - timedelta(days=age_days),
                    rank=1,
                    query="mirrors",
                )
            )
        session.commit()
        housekeeping(session)
    with SessionLocal() as session:
        remaining = list(
            session.scalars(select(MemoryRecall).where(MemoryRecall.memory_id == UUID(memory_id)))
        )
        assert len(remaining) == 1
        assert remaining[0].recalled_at.replace(tzinfo=UTC) > now - timedelta(days=1)


async def test_a_trace_cannot_record_the_same_memory_twice(
    client: httpx.AsyncClient, identity
) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import IntegrityError

    _key, organization, _workspace = identity
    memory_id = await _remember(client, "Neck strips are replaced for every client.")
    trace_id = uuid4()
    with SessionLocal() as session:
        for _ in range(2):
            session.add(
                MemoryRecall(
                    memory_id=UUID(memory_id),
                    organization_id=organization.id,
                    trace_id=trace_id,
                    recalled_at=datetime.now(UTC),
                    rank=1,
                    query="strips",
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()

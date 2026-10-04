from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from albert.config import get_settings
from albert.db import SessionLocal
from albert.models import ConsoleState, Memory, MemoryLink, MemoryRecall, MemoryStat


def _memory(session, organization_id, content: str) -> Memory:  # type: ignore[no-untyped-def]
    memory = Memory(
        organization_id=organization_id,
        owner_principal_id=uuid4(),
        subject=content[:40],
        content=content,
    )
    session.add(memory)
    session.flush()
    return memory


def test_link_tables_round_trip_and_cascade() -> None:
    organization_id = uuid4()
    with SessionLocal() as session:
        first = _memory(session, organization_id, "first memory")
        second = _memory(session, organization_id, "second memory")
        session.add_all(
            [
                MemoryLink(
                    organization_id=organization_id,
                    source_memory_id=first.id,
                    target_memory_id=second.id,
                    kind="similar",
                    weight=0.91,
                ),
                MemoryRecall(
                    memory_id=first.id,
                    organization_id=organization_id,
                    trace_id=uuid4(),
                    recalled_at=datetime.now(UTC),
                    rank=1,
                    query="what is first",
                ),
                MemoryStat(memory_id=first.id, organization_id=organization_id, recall_count=3),
                ConsoleState(key=f"test-{organization_id}", value={"cursor": "x"}),
            ]
        )
        session.commit()
        first_id = first.id
        link = session.scalar(select(MemoryLink).where(MemoryLink.source_memory_id == first_id))
        assert link is not None and link.count == 1 and link.kind == "similar"
        session.delete(first)
        session.commit()
    with SessionLocal() as session:
        for model, column in (
            (MemoryLink, MemoryLink.source_memory_id),
            (MemoryRecall, MemoryRecall.memory_id),
            (MemoryStat, MemoryStat.memory_id),
        ):
            remaining = session.scalar(
                select(func.count()).select_from(model).where(column == first_id)
            )
            assert remaining == 0, model.__tablename__


def test_link_settings_defaults() -> None:
    settings = get_settings()
    assert settings.link_neighbors == 5
    assert settings.link_similarity_min == 0.78
    assert settings.housekeeping_seconds == 60
    assert settings.map_node_limit == 1500

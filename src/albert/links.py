"""Derived links between memories.

`similar` and `sequence` links are written when a memory is indexed; `recalled`
links and recall statistics come from the flight recorder's traces. Everything
here is rebuildable and must never fail the indexing that triggers it.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import Float, cast, or_, select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.models import Episode, Memory, MemoryChunk, MemoryLink
from albert.recorder import span
from albert.search import _cosine, _enable_iterative_scan

logger = logging.getLogger("albert.links")
PROBE_CHUNKS = 8
DATED_NAME = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_postgres(session: Session) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


def _pair(first: UUID, second: UUID) -> tuple[UUID, UUID]:
    """Canonical order for an undirected link: smaller id first."""
    return (first, second) if str(first) < str(second) else (second, first)


def nearest_memories(
    session: Session, memory: Memory, *, k: int, minimum: float
) -> list[tuple[UUID, float]]:
    """Other active memories in the same workspace, by best chunk-pair cosine similarity."""
    chunks = list(
        session.scalars(
            select(MemoryChunk)
            .where(MemoryChunk.memory_id == memory.id, MemoryChunk.embedding.is_not(None))
            .order_by(MemoryChunk.chunk_index)
        )
    )
    if not chunks:
        return []
    scope = [
        MemoryChunk.organization_id == memory.organization_id,
        MemoryChunk.memory_id != memory.id,
        MemoryChunk.embedding_model == chunks[0].embedding_model,
        MemoryChunk.embedding.is_not(None),
        Memory.status == "active",
        (
            MemoryChunk.workspace_id == memory.workspace_id
            if memory.workspace_id is not None
            else MemoryChunk.workspace_id.is_(None)
        ),
    ]
    best: dict[UUID, float] = {}
    if _is_postgres(session):
        _enable_iterative_scan(session)
        for chunk in chunks[:PROBE_CHUNKS]:
            distance = cast(MemoryChunk.embedding.op("<=>")(chunk.embedding), Float)
            rows = session.execute(
                select(MemoryChunk.memory_id, distance)
                .join(Memory, Memory.id == MemoryChunk.memory_id)
                .where(*scope)
                .order_by(distance)
                .limit(k * 8)
            )
            for other_id, value in rows:
                similarity = 1.0 - float(value)
                if similarity > best.get(other_id, -1.0):
                    best[other_id] = similarity
    else:
        others = session.execute(
            select(MemoryChunk.memory_id, MemoryChunk.embedding)
            .join(Memory, Memory.id == MemoryChunk.memory_id)
            .where(*scope)
        ).all()
        mine = [list(chunk.embedding) for chunk in chunks[:PROBE_CHUNKS]]
        for other_id, embedding in others:
            similarity = max(_cosine(list(embedding or []), vector) for vector in mine)
            if similarity > best.get(other_id, -1.0):
                best[other_id] = similarity
    ranked = sorted(best.items(), key=lambda item: (-item[1], str(item[0])))
    return [(other_id, score) for other_id, score in ranked if score >= minimum][:k]


def _touching(memory_id: UUID, kind: str) -> Any:
    return (
        MemoryLink.kind == kind,
        or_(MemoryLink.source_memory_id == memory_id, MemoryLink.target_memory_id == memory_id),
    )


def drop_links_for_memory(session: Session, memory_id: UUID) -> None:
    """Remove every link touching a memory (used when it is forgotten)."""
    session.query(MemoryLink).filter(
        or_(MemoryLink.source_memory_id == memory_id, MemoryLink.target_memory_id == memory_id)
    ).delete(synchronize_session=False)


def refresh_similar_links(session: Session, memory: Memory) -> int:
    """Replace the memory's `similar` links with its current nearest neighbours."""
    settings = get_settings()
    session.query(MemoryLink).filter(*_touching(memory.id, "similar")).delete(
        synchronize_session=False
    )
    session.flush()
    neighbours = nearest_memories(
        session, memory, k=settings.link_neighbors, minimum=settings.link_similarity_min
    )
    for other_id, similarity in neighbours:
        source, target = _pair(memory.id, other_id)
        session.add(
            MemoryLink(
                organization_id=memory.organization_id,
                workspace_id=memory.workspace_id,
                source_memory_id=source,
                target_memory_id=target,
                kind="similar",
                weight=round(similarity, 4),
            )
        )
    session.flush()
    return len(neighbours)


def _dated_parent(source_uri: str) -> str | None:
    """The directory of a dated source file, or None when the name carries no date."""
    parent, _, name = source_uri.rpartition("/")
    return parent + "/" if parent and DATED_NAME.search(name) else None


def link_sequence(session: Session, memory: Memory) -> int:
    """Link the previous dated entry from the same source directory to this memory."""
    if memory.episode_id is None:
        return 0
    episode = session.get(Episode, memory.episode_id)
    if episode is None:
        return 0
    parent = _dated_parent(episode.source_uri)
    if parent is None:
        return 0
    candidates = session.scalars(
        select(Episode)
        .where(
            Episode.organization_id == episode.organization_id,
            (
                Episode.workspace_id == episode.workspace_id
                if episode.workspace_id is not None
                else Episode.workspace_id.is_(None)
            ),
            Episode.id != episode.id,
            Episode.deleted_at.is_(None),
            Episode.source_uri.like(f"{parent}%"),
            Episode.occurred_at < episode.occurred_at,
        )
        .order_by(Episode.occurred_at.desc())
        .limit(20)
    )
    for previous in candidates:
        if _dated_parent(previous.source_uri) != parent:
            continue  # a deeper directory or an undated sibling
        earlier = session.scalar(
            select(Memory).where(Memory.episode_id == previous.id, Memory.status == "active")
        )
        if earlier is None:
            continue
        exists = session.scalar(
            select(MemoryLink.id).where(
                MemoryLink.source_memory_id == earlier.id,
                MemoryLink.target_memory_id == memory.id,
                MemoryLink.kind == "sequence",
            )
        )
        if exists is None:
            session.add(
                MemoryLink(
                    organization_id=memory.organization_id,
                    workspace_id=memory.workspace_id,
                    source_memory_id=earlier.id,
                    target_memory_id=memory.id,
                    kind="sequence",
                    weight=1.0,
                )
            )
            session.flush()
        return 1
    return 0


def refresh_links_for_memory(session: Session, memory: Memory) -> dict[str, int]:
    """Compute a memory's links inside a `link` span; never raise."""
    result = {"similar": 0, "sequence": 0}
    with span("link") as handle:
        try:
            with session.begin_nested():
                result["similar"] = refresh_similar_links(session, memory)
                result["sequence"] = link_sequence(session, memory)
        except Exception as exc:
            logger.exception("link computation failed for memory %s", memory.id)
            handle.set(error=type(exc).__name__)
        handle.set(**result)
    return result


def rebuild_links(session: Session, organization_id: UUID | None = None) -> dict[str, int]:
    """Recompute `similar` and `sequence` links for every active memory in scope.

    Unlike incremental indexing this yields the exact union of every memory's
    nearest neighbours, including links a newer memory would add to an older one.
    """
    settings = get_settings()
    scope = [MemoryLink.kind.in_(("similar", "sequence"))]
    memory_scope = [Memory.status == "active"]
    if organization_id is not None:
        scope.append(MemoryLink.organization_id == organization_id)
        memory_scope.append(Memory.organization_id == organization_id)
    session.query(MemoryLink).filter(*scope).delete(synchronize_session=False)
    session.flush()
    memories = list(session.scalars(select(Memory).where(*memory_scope).order_by(Memory.id)))
    pairs: dict[tuple[UUID, UUID], tuple[Memory, float]] = {}
    for memory in memories:
        for other_id, similarity in nearest_memories(
            session, memory, k=settings.link_neighbors, minimum=settings.link_similarity_min
        ):
            key = _pair(memory.id, other_id)
            if key not in pairs or similarity > pairs[key][1]:
                pairs[key] = (memory, similarity)
    for (source, target), (memory, similarity) in pairs.items():
        session.add(
            MemoryLink(
                organization_id=memory.organization_id,
                workspace_id=memory.workspace_id,
                source_memory_id=source,
                target_memory_id=target,
                kind="similar",
                weight=round(similarity, 4),
            )
        )
    session.flush()
    sequence = sum(link_sequence(session, memory) for memory in memories)
    return {"memories": len(memories), "similar": len(pairs), "sequence": sequence}

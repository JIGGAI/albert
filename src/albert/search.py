from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Float, cast, func, literal_column, or_, select, text
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.embeddings import get_embedder
from albert.graph_store import get_graph_store
from albert.models import Memory, MemoryChunk
from albert.recorder import cap_candidates, current_recorder, span
from albert.schemas import SearchHit, SearchRequest, SearchResponse

RRF_K = 60
logger = logging.getLogger("albert.search")


def _scope_conditions(
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    request: SearchRequest,
) -> list[Any]:
    conditions: list[Any] = [
        Memory.organization_id == organization_id,
        Memory.status == "active",
    ]
    if workspace_id is not None:
        conditions.append(Memory.workspace_id == workspace_id)
    if request.project_ref is not None:
        conditions.append(Memory.project_ref == request.project_ref)
    if request.task_ref is not None:
        conditions.append(Memory.task_ref == request.task_ref)
    if request.run_ref is not None:
        conditions.append(Memory.run_ref == request.run_ref)
    if request.memory_types:
        conditions.append(Memory.memory_type.in_(request.memory_types))
    if request.sensitivity:
        conditions.append(Memory.sensitivity.in_(request.sensitivity))
    when = request.temporal_as_of or datetime.now(UTC)
    conditions.extend(
        [Memory.valid_from <= when, or_(Memory.valid_until.is_(None), Memory.valid_until > when)]
    )
    return conditions


def _lexical_search(
    session: Session,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    request: SearchRequest,
) -> list[tuple[Memory, float]]:
    conditions = _scope_conditions(
        organization_id=organization_id, workspace_id=workspace_id, request=request
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # memories.search_vector is a stored generated tsvector (migration 0003)
        # with a GIN index; matching on the column keeps the index usable and
        # avoids recomputing to_tsvector for every ranked row.
        document = literal_column("memories.search_vector")
        query = func.plainto_tsquery(literal_column("'english'::regconfig"), request.query)
        rank = func.ts_rank_cd(document, query)
        statement = (
            select(Memory, rank.label("lexical_rank"))
            .where(*conditions, document.op("@@")(query))
            .order_by(rank.desc())
            .limit(request.limit * 3)
        )
        return [(memory, float(score)) for memory, score in session.execute(statement)]

    terms = [term for term in re.findall(r"[\w.-]+", request.query.casefold()) if term]
    rows = list(session.scalars(select(Memory).where(*conditions)))

    def score(memory: Memory) -> int:
        text = f"{memory.subject} {memory.content}".casefold()
        return sum(text.count(term) for term in terms)

    ranked = [(row, float(score(row))) for row in rows if score(row) > 0]
    return sorted(ranked, key=lambda item: item[1], reverse=True)[: request.limit * 3]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / denominator if denominator else -1


def _enable_iterative_scan(session: Session) -> None:
    """Let pgvector keep scanning until the filtered result is full.

    An HNSW scan returns ef_search candidates and only then applies the tenant,
    workspace and sensitivity predicates; a small tenant inside a large table
    can otherwise get zero vector hits. relaxed_order may return rows slightly
    out of distance order, so callers re-sort.
    """
    try:
        with session.begin_nested():
            session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    except Exception:  # pragma: no cover - older pgvector without the GUC
        logger.warning("hnsw.iterative_scan unavailable; small tenants may lose vector hits")


def _vector_search(
    session: Session,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    request: SearchRequest,
) -> list[tuple[Memory, float, int]]:
    """Best-matching chunk per memory as (memory, similarity, chunk_index)."""
    embedder = get_embedder()
    query_vector = embedder.embed(request.query)
    conditions = _scope_conditions(
        organization_id=organization_id, workspace_id=workspace_id, request=request
    )
    # Vectors from another model live in a different space; comparing them to
    # this query would rank noise. They are re-embedded by reindex-memories.
    conditions.extend(
        [
            MemoryChunk.organization_id == organization_id,
            MemoryChunk.embedding_model == embedder.name,
            MemoryChunk.embedding.is_not(None),
        ]
    )
    minimum = get_settings().min_vector_similarity
    candidate_limit = request.limit * 6
    best: dict[UUID, tuple[Memory, float, int]] = {}

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        _enable_iterative_scan(session)
        distance = cast(MemoryChunk.embedding.op("<=>")(query_vector), Float)
        rows = session.execute(
            select(Memory, MemoryChunk.chunk_index, distance.label("vector_distance"))
            .join(MemoryChunk, MemoryChunk.memory_id == Memory.id)
            .where(*conditions)
            .order_by(distance)
            .limit(candidate_limit)
        )
        scored = [(memory, 1.0 - float(value), index) for memory, index, value in rows]
    else:
        rows = session.execute(
            select(Memory, MemoryChunk.chunk_index, MemoryChunk.embedding)
            .join(MemoryChunk, MemoryChunk.memory_id == Memory.id)
            .where(*conditions)
        )
        scored = [
            (memory, _cosine(embedding or [], query_vector), index)
            for memory, index, embedding in rows
        ]

    for memory, similarity, index in scored:
        if similarity < minimum:
            continue
        current = best.get(memory.id)
        if current is None or similarity > current[1]:
            best[memory.id] = (memory, similarity, index)
    ordered = sorted(best.values(), key=lambda item: item[1], reverse=True)
    return ordered[: request.limit * 3]


def _filters(request: SearchRequest, workspace_id: UUID | None) -> dict[str, Any]:
    return {
        "workspace_id": str(workspace_id) if workspace_id else None,
        "project_ref": request.project_ref,
        "task_ref": request.task_ref,
        "run_ref": request.run_ref,
        "memory_types": request.memory_types,
        "sensitivity": request.sensitivity,
        "temporal_as_of": request.temporal_as_of.isoformat() if request.temporal_as_of else None,
    }


def _candidates(items: list[tuple[Any, float]], kind: str) -> list[dict[str, Any]]:
    return cap_candidates(
        [
            {"id": str(ident), "kind": kind, "score": score, "rank": rank}
            for rank, (ident, score) in enumerate(items, start=1)
        ]
    )


def _memory_hit(memory: Memory, request: SearchRequest, backend: str, score: float) -> SearchHit:
    return SearchHit(
        memory_id=memory.id,
        kind="memory",
        subject=memory.subject,
        content=memory.content if request.include_content else "",
        score=0,
        backends=[],
        source_episode_id=memory.episode_id,
        metadata={
            "memory_type": memory.memory_type,
            "sensitivity": memory.sensitivity,
            "confidence": memory.confidence,
            "backend_scores": {backend: score},
        },
    )


def hybrid_search(
    session: Session,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    request: SearchRequest,
    allowed_sensitivities: list[str],
) -> SearchResponse:
    ranked: list[tuple[str, list[tuple[str, SearchHit]]]] = []
    degraded: list[str] = []
    filters = _filters(request, workspace_id)

    with span("lexical", filters=filters) as handle:
        lexical = _lexical_search(
            session, organization_id=organization_id, workspace_id=workspace_id, request=request
        )
        handle.set(
            candidates_total=len(lexical),
            candidates=_candidates([(m.id, s) for m, s in lexical], "memory"),
        )
    ranked.append(
        (
            "lexical",
            [(f"memory:{m.id}", _memory_hit(m, request, "lexical", s)) for m, s in lexical],
        )
    )

    try:
        with span(
            "vector",
            filters=filters,
            embedding_model=get_embedder().name,
            min_similarity=get_settings().min_vector_similarity,
        ) as handle:
            vectors = _vector_search(
                session,
                organization_id=organization_id,
                workspace_id=workspace_id,
                request=request,
            )
            handle.set(
                candidates_total=len(vectors),
                candidates=_candidates([(m.id, s) for m, s, _i in vectors], "memory"),
            )
        vector_hits = []
        for memory, similarity, chunk_index in vectors:
            hit = _memory_hit(memory, request, "vector", similarity)
            hit.metadata["chunk_index"] = chunk_index
            vector_hits.append((f"memory:{memory.id}", hit))
        ranked.append(("vector", vector_hits))
    except Exception as exc:
        logger.exception("Vector retrieval failed")
        degraded.append(f"vector retrieval unavailable: {type(exc).__name__}")

    if request.include_graph:
        try:
            with span("graph", filters=filters) as handle:
                relationships = get_graph_store().query(
                    session,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    query=request.query,
                    temporal_as_of=request.temporal_as_of,
                    limit=request.limit * 3,
                    sensitivities=allowed_sensitivities,
                )
                handle.set(
                    candidates_total=len(relationships),
                    candidates=_candidates(
                        [(r.id, r.confidence) for r in relationships], "relationship"
                    ),
                )
            ranked.append(
                (
                    "graph",
                    [
                        (
                            f"relationship:{relationship.id}",
                            SearchHit(
                                memory_id=relationship.source_memory_id,
                                kind="relationship",
                                subject=relationship.relation_type,
                                content=(
                                    f"{relationship.source_name} "
                                    f"{relationship.relation_type.replace('_', ' ')} "
                                    f"{relationship.target_name}"
                                    if request.include_content
                                    else ""
                                ),
                                score=0,
                                backends=[],
                                metadata={
                                    "relationship_id": str(relationship.id),
                                    "source_entity_id": str(relationship.source_entity_id),
                                    "target_entity_id": str(relationship.target_entity_id),
                                    "confidence": relationship.confidence,
                                    "backend_scores": {"graph": relationship.confidence},
                                },
                            ),
                        )
                        for relationship in relationships
                    ],
                )
            )
        except Exception as exc:
            logger.exception("Graph retrieval failed")
            degraded.append(f"graph retrieval unavailable: {type(exc).__name__}")

    with span("fuse", k=RRF_K) as handle:
        scores: dict[str, float] = defaultdict(float)
        contributions: dict[str, dict[str, float]] = defaultdict(dict)
        hits: dict[str, SearchHit] = {}
        backend_names: dict[str, list[str]] = defaultdict(list)
        for backend, backend_hits in ranked:
            for rank, (key, hit) in enumerate(backend_hits, start=1):
                contribution = 1.0 / (RRF_K + rank)
                scores[key] += contribution
                contributions[key][backend] = round(contribution, 6)
                if key in hits:
                    existing_scores = hits[key].metadata.setdefault("backend_scores", {})
                    existing_scores.update(hit.metadata.get("backend_scores", {}))
                    for name, value in hit.metadata.items():
                        hits[key].metadata.setdefault(name, value)
                else:
                    hits[key] = hit
                backend_names[key].append(backend)
        ordered = sorted(hits, key=lambda key: scores[key], reverse=True)[: request.limit]
        result = []
        for key in ordered:
            hit = hits[key]
            hit.score = scores[key]
            hit.backends = backend_names[key]
            result.append(hit)
        handle.set(
            candidates_total=len(hits),
            hits=[
                {
                    "id": key.split(":", 1)[1],
                    "kind": key.split(":", 1)[0],
                    "rank": rank,
                    "rrf": contributions[key],
                    "backends": backend_names[key],
                }
                for rank, key in enumerate(ordered, start=1)
            ],
            degraded=degraded,
        )
    if degraded:
        recorder = current_recorder()
        if recorder is not None:
            recorder.status = "degraded"
    return SearchResponse(hits=result, degraded=degraded)

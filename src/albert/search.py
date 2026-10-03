from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from albert.embeddings import get_embedder
from albert.graph import query_relationships
from albert.models import Memory
from albert.schemas import SearchHit, SearchRequest, SearchResponse

RRF_K = 60


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
) -> list[Memory]:
    conditions = _scope_conditions(
        organization_id=organization_id, workspace_id=workspace_id, request=request
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        document = func.to_tsvector(
            "english", func.coalesce(Memory.subject, "") + " " + func.coalesce(Memory.content, "")
        )
        query = func.plainto_tsquery("english", request.query)
        rank = func.ts_rank_cd(document, query)
        statement = (
            select(Memory)
            .where(*conditions, document.op("@@")(query))
            .order_by(rank.desc())
            .limit(request.limit * 3)
        )
        return list(session.scalars(statement))

    terms = [term for term in re.findall(r"[\w.-]+", request.query.casefold()) if term]
    rows = list(session.scalars(select(Memory).where(*conditions)))

    def score(memory: Memory) -> int:
        text = f"{memory.subject} {memory.content}".casefold()
        return sum(text.count(term) for term in terms)

    return [row for row in sorted(rows, key=score, reverse=True) if score(row) > 0][
        : request.limit * 3
    ]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / denominator if denominator else -1


def _vector_search(
    session: Session,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    request: SearchRequest,
) -> list[Memory]:
    query_vector = get_embedder().embed(request.query)
    conditions = _scope_conditions(
        organization_id=organization_id, workspace_id=workspace_id, request=request
    )
    conditions.append(Memory.embedding.is_not(None))
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        distance = Memory.embedding.op("<=>")(query_vector)
        return list(
            session.scalars(
                select(Memory)
                .where(*conditions)
                .order_by(distance)
                .limit(request.limit * 3)
            )
        )
    rows = list(session.scalars(select(Memory).where(*conditions)))
    rows.sort(key=lambda memory: _cosine(memory.embedding or [], query_vector), reverse=True)
    return rows[: request.limit * 3]


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

    lexical = _lexical_search(
        session, organization_id=organization_id, workspace_id=workspace_id, request=request
    )
    ranked.append(
        (
            "lexical",
            [
                (
                    f"memory:{memory.id}",
                    SearchHit(
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
                        },
                    ),
                )
                for memory in lexical
            ],
        )
    )

    try:
        vectors = _vector_search(
            session, organization_id=organization_id, workspace_id=workspace_id, request=request
        )
        ranked.append(
            (
                "vector",
                [
                    (
                        f"memory:{memory.id}",
                        SearchHit(
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
                            },
                        ),
                    )
                    for memory in vectors
                ],
            )
        )
    except Exception as exc:
        degraded.append(f"vector retrieval unavailable: {type(exc).__name__}")

    if request.include_graph:
        try:
            relationships = query_relationships(
                session,
                organization_id=organization_id,
                workspace_id=workspace_id,
                query=request.query,
                temporal_as_of=request.temporal_as_of,
                limit=request.limit * 3,
                sensitivities=allowed_sensitivities,
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
                                },
                            ),
                        )
                        for relationship in relationships
                    ],
                )
            )
        except Exception as exc:
            degraded.append(f"graph retrieval unavailable: {type(exc).__name__}")

    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, SearchHit] = {}
    backend_names: dict[str, list[str]] = defaultdict(list)
    for backend, backend_hits in ranked:
        for rank, (key, hit) in enumerate(backend_hits, start=1):
            scores[key] += 1.0 / (RRF_K + rank)
            hits.setdefault(key, hit)
            backend_names[key].append(backend)
    ordered = sorted(hits, key=lambda key: scores[key], reverse=True)[: request.limit]
    result = []
    for key in ordered:
        hit = hits[key]
        hit.score = scores[key]
        hit.backends = backend_names[key]
        result.append(hit)
    return SearchResponse(hits=result, degraded=degraded)

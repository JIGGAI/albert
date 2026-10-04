from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from albert import __version__
from albert.config import get_settings
from albert.console import router as console_router
from albert.db import get_session
from albert.graph import query_relationships, subgraph
from albert.models import Entity, Episode, Relationship
from albert.portability import export_bundle, import_bundle
from albert.recorder import CANDIDATE_LIMIT, cap_candidates, current_recorder, span
from albert.schemas import (
    ContextRequest,
    ContextResponse,
    EpisodeCreate,
    EpisodeRead,
    ExportBundle,
    GraphQuery,
    HealthResponse,
    ImportRequest,
    ImportResult,
    LockAcquire,
    LockRead,
    LockRelease,
    LockRenew,
    MemoryCreate,
    MemoryRead,
    MemoryUpdate,
    RelationshipCreate,
    RelationshipRead,
    SearchRequest,
    SearchResponse,
    SubgraphQuery,
    WorkingMemoryCreate,
    WorkingMemoryCreateRead,
    WorkingMemoryFinish,
    WorkingMemoryRead,
    WorkingMemoryUpdate,
)
from albert.search import hybrid_search
from albert.security import AuthContext, authenticate
from albert.services import (
    acquire_lock,
    add_explicit_relationship,
    audit,
    create_episode,
    create_memory,
    create_working_memory,
    delete_episode,
    delete_memory,
    finish_working_memory,
    get_episode,
    get_memory,
    get_working_memory,
    release_lock,
    renew_lock,
    resolve_workspace,
    update_memory,
    update_working_memory,
)
from albert.trace_writer import get_trace_writer
from albert.tracing import RecordingMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("albert.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    logger.info("Albert API starting", extra={"version": __version__})
    writer = get_trace_writer()
    writer.start()
    yield
    writer.stop()


app = FastAPI(
    title="Albert Memory API",
    version=__version__,
    description="Independent hybrid memory service for AI agents",
    lifespan=lifespan,
)
app.add_middleware(RecordingMiddleware)
app.include_router(console_router)


def _memory_ids(result: SearchResponse) -> list[str]:
    """Final memory hits in rank order, ids only, for the trace summary."""
    ids: list[str] = []
    for hit in result.hits:
        if hit.memory_id is not None and str(hit.memory_id) not in ids:
            ids.append(str(hit.memory_id))
    return ids[:CANDIDATE_LIMIT]


def _note(**fields: object) -> None:
    recorder = current_recorder()
    if recorder is not None:
        recorder.note(**fields)


@app.get("/v1/health/live", response_model=HealthResponse, tags=["health"])
def live() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        database="unchecked",
        version=__version__,
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        classifier_provider=settings.llm_provider,
    )


@app.get("/v1/health/ready", response_model=HealthResponse, tags=["health"])
def ready(session: Session = Depends(get_session)) -> HealthResponse:
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    settings = get_settings()
    return HealthResponse(
        status="ok",
        database="ok",
        version=__version__,
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        classifier_provider=settings.llm_provider,
    )


@app.post("/v1/episodes", response_model=EpisodeRead, status_code=201, tags=["episodes"])
def post_episode(
    data: EpisodeCreate,
    response: Response,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> Episode:
    episode, created = create_episode(session, auth, data)
    if not created:
        response.status_code = status.HTTP_200_OK
    return episode


@app.get("/v1/episodes/{episode_id}", response_model=EpisodeRead, tags=["episodes"])
def read_episode(
    episode_id: UUID,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> Episode:
    episode = get_episode(session, auth, episode_id)
    audit(session, auth, "episode.read", resource_type="episode", resource_id=str(episode.id))
    session.commit()
    return episode


@app.delete("/v1/episodes/{episode_id}", status_code=204, tags=["episodes"])
def remove_episode(
    episode_id: UUID,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> Response:
    delete_episode(session, auth, episode_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/v1/memories", response_model=MemoryRead, status_code=201, tags=["memories"])
def post_memory(
    data: MemoryCreate,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    memory = create_memory(session, auth, data)
    _note(stored_ids=[str(memory.id)])
    return memory


@app.get("/v1/memories/{memory_id}", response_model=MemoryRead, tags=["memories"])
def read_memory(
    memory_id: UUID,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    memory = get_memory(session, auth, memory_id)
    audit(session, auth, "memory.read", resource_type="memory", resource_id=str(memory.id))
    session.commit()
    return memory


@app.patch("/v1/memories/{memory_id}", response_model=MemoryRead, tags=["memories"])
def patch_memory(
    memory_id: UUID,
    data: MemoryUpdate,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    return update_memory(session, auth, memory_id, data)


@app.delete("/v1/memories/{memory_id}", status_code=204, tags=["memories"])
def remove_memory(
    memory_id: UUID,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> Response:
    delete_memory(session, auth, memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/v1/search", response_model=SearchResponse, tags=["retrieval"])
def search_memories(
    data: SearchRequest,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> SearchResponse:
    auth.require("memory.read")
    workspace_id = resolve_workspace(session, auth, data.workspace_id)
    allowed_sensitivities = auth.allowed_sensitivities()
    if data.sensitivity and not set(data.sensitivity).issubset(allowed_sensitivities):
        raise HTTPException(status_code=403, detail="Requested sensitivity is not authorized")
    effective = data.model_copy(update={"sensitivity": data.sensitivity or allowed_sensitivities})
    _note(query=data.query)
    result = hybrid_search(
        session,
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        request=effective,
        allowed_sensitivities=allowed_sensitivities,
    )
    _note(hits=len(result.hits), degraded=result.degraded, memory_ids=_memory_ids(result))
    audit(
        session,
        auth,
        "memory.searched",
        detail={"result_count": len(result.hits), "degraded": result.degraded},
    )
    session.commit()
    return result


@app.post("/v1/context/assemble", response_model=ContextResponse, tags=["retrieval"])
def assemble_context(
    data: ContextRequest,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> ContextResponse:
    auth.require("memory.read")
    workspace_id = resolve_workspace(session, auth, data.workspace_id)
    allowed_sensitivities = auth.allowed_sensitivities()
    if data.sensitivity and not set(data.sensitivity).issubset(allowed_sensitivities):
        raise HTTPException(status_code=403, detail="Requested sensitivity is not authorized")
    effective = data.model_copy(update={"sensitivity": data.sensitivity or allowed_sensitivities})
    _note(query=data.query)
    result = hybrid_search(
        session,
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        request=effective,
        allowed_sensitivities=allowed_sensitivities,
    )
    _note(hits=len(result.hits), degraded=result.degraded, memory_ids=_memory_ids(result))
    sections: list[str] = []
    citations: list[dict] = []
    used = 0
    for index, hit in enumerate(result.hits, start=1):
        section = f"[{index}] {hit.subject}\n{hit.content}".strip()
        if used + len(section) > data.max_characters:
            break
        sections.append(section)
        used += len(section) + 2
        citations.append(
            {
                "index": index,
                "kind": hit.kind,
                "memory_id": str(hit.memory_id) if hit.memory_id else None,
                "episode_id": str(hit.source_episode_id) if hit.source_episode_id else None,
                "backends": hit.backends,
            }
        )
    audit(
        session,
        auth,
        "context.assembled",
        detail={"citations": len(citations), "characters": used, "degraded": result.degraded},
    )
    session.commit()
    return ContextResponse(
        context="\n\n".join(sections), citations=citations, degraded=result.degraded
    )


def _relationship_read(relationship: Relationship) -> RelationshipRead:
    return RelationshipRead(
        id=relationship.id,
        source_entity_id=relationship.source.id,
        source_name=relationship.source.display_name,
        source_type=relationship.source.entity_type,
        relation_type=relationship.relation_type,
        sensitivity=relationship.sensitivity,
        target_entity_id=relationship.target.id,
        target_name=relationship.target.display_name,
        target_type=relationship.target.entity_type,
        source_memory_id=relationship.source_memory_id,
        confidence=relationship.confidence,
        valid_from=relationship.valid_from,
        valid_until=relationship.valid_until,
    )


@app.post("/v1/relationships", response_model=RelationshipRead, status_code=201, tags=["graph"])
def post_relationship(
    data: RelationshipCreate,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> RelationshipRead:
    return _relationship_read(add_explicit_relationship(session, auth, data))


@app.post("/v1/graph/query", response_model=list[RelationshipRead], tags=["graph"])
def query_graph(
    data: GraphQuery,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> list[RelationshipRead]:
    auth.require("graph.query")
    workspace_id = resolve_workspace(session, auth, data.workspace_id)
    _note(query=data.query)
    with span("graph") as handle:
        result = query_relationships(
            session,
            organization_id=auth.organization_id,
            workspace_id=workspace_id,
            query=data.query,
            temporal_as_of=data.temporal_as_of,
            limit=data.limit,
            sensitivities=auth.allowed_sensitivities(),
        )
        handle.set(
            candidates_total=len(result),
            candidates=cap_candidates(
                [
                    {"id": str(r.id), "kind": "relationship", "score": r.confidence, "rank": i}
                    for i, r in enumerate(result, start=1)
                ]
            ),
        )
    _note(hits=len(result))
    audit(session, auth, "graph.queried", detail={"result_count": len(result)})
    session.commit()
    return result


@app.get(
    "/v1/graph/entities/{entity_id}/subgraph",
    response_model=list[RelationshipRead],
    tags=["graph"],
)
def get_subgraph(
    entity_id: UUID,
    query: SubgraphQuery = Depends(),
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> list[RelationshipRead]:
    auth.require("graph.query")
    entity = session.scalar(
        select(Entity).where(Entity.id == entity_id, Entity.organization_id == auth.organization_id)
    )
    if entity is None or (
        auth.workspace_id is not None and entity.workspace_id != auth.workspace_id
    ):
        raise HTTPException(status_code=404, detail="Entity not found")
    return subgraph(
        session,
        organization_id=auth.organization_id,
        workspace_id=entity.workspace_id,
        entity_id=entity.id,
        hops=query.hops,
        temporal_as_of=query.temporal_as_of,
        limit=query.limit,
        sensitivities=auth.allowed_sensitivities(),
    )


@app.post(
    "/v1/working-memory",
    response_model=WorkingMemoryCreateRead,
    status_code=201,
    tags=["working-memory"],
)
def post_working_memory(
    data: WorkingMemoryCreate,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    working, lock_result = create_working_memory(session, auth, data)
    lock_read = None
    if lock_result is not None:
        lock, token = lock_result
        lock_read = LockRead(
            id=lock.id,
            resource_type=lock.resource_type,
            resource_ref=lock.resource_ref,
            fence=lock.fence,
            expires_at=lock.expires_at,
            token=token,
        )
    return WorkingMemoryCreateRead(
        **WorkingMemoryRead.model_validate(working).model_dump(), lock=lock_read
    )


@app.patch(
    "/v1/working-memory/{working_id}",
    response_model=WorkingMemoryRead,
    tags=["working-memory"],
)
def patch_working_memory(
    working_id: UUID,
    data: WorkingMemoryUpdate,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    return update_working_memory(session, auth, working_id, data)


@app.get(
    "/v1/working-memory/{working_id}",
    response_model=WorkingMemoryRead,
    tags=["working-memory"],
)
def read_working_memory(
    working_id: UUID,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    if "working_memory.read" not in auth.capabilities:
        auth.require("working_memory.write")
    return get_working_memory(session, auth, working_id)


@app.post(
    "/v1/working-memory/{working_id}/complete",
    response_model=WorkingMemoryRead,
    tags=["working-memory"],
)
def complete_working_memory(
    working_id: UUID,
    data: WorkingMemoryFinish,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    return finish_working_memory(session, auth, working_id, data, failed=False)


@app.post(
    "/v1/working-memory/{working_id}/fail",
    response_model=WorkingMemoryRead,
    tags=["working-memory"],
)
def fail_working_memory(
    working_id: UUID,
    data: WorkingMemoryFinish,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
):  # type: ignore[no-untyped-def]
    return finish_working_memory(session, auth, working_id, data, failed=True)


@app.post("/v1/locks/acquire", response_model=LockRead, status_code=201, tags=["locks"])
def post_lock(
    data: LockAcquire,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> LockRead:
    lock, token = acquire_lock(session, auth, data)
    return LockRead(
        id=lock.id,
        resource_type=lock.resource_type,
        resource_ref=lock.resource_ref,
        fence=lock.fence,
        expires_at=lock.expires_at,
        token=token,
    )


@app.get("/v1/export", response_model=ExportBundle, tags=["portability"])
def get_export(
    workspace_id: UUID | None = None,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> ExportBundle:
    return export_bundle(session, auth, workspace_id)


@app.post("/v1/import", response_model=ImportResult, tags=["portability"])
def post_import(
    data: ImportRequest,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> ImportResult:
    return import_bundle(session, auth, data)


@app.post("/v1/locks/{lock_id}/renew", response_model=LockRead, tags=["locks"])
def post_lock_renew(
    lock_id: UUID,
    data: LockRenew,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> LockRead:
    lock = renew_lock(
        session,
        auth,
        lock_id,
        token=data.token,
        fence=data.fence,
        ttl_seconds=data.ttl_seconds,
    )
    return LockRead(
        id=lock.id,
        resource_type=lock.resource_type,
        resource_ref=lock.resource_ref,
        fence=lock.fence,
        expires_at=lock.expires_at,
    )


@app.post("/v1/locks/{lock_id}/release", status_code=204, tags=["locks"])
def post_lock_release(
    lock_id: UUID,
    data: LockRelease,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> Response:
    release_lock(session, auth, lock_id, token=data.token, fence=data.fence)
    return Response(status_code=204)


def run() -> None:
    uvicorn.run("albert.api:app", host="0.0.0.0", port=8080)

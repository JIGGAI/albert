"""Operator read API for the memory console.

Everything here is read-only and operator-scoped: it crosses tenants on purpose
and is gated by the console.read capability. Memory content is returned only by
the explicit detail endpoints, truncated and audited.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.db import SessionLocal, engine, get_session
from albert.models import Entity, Job, Memory, Relationship, Trace
from albert.security import AuthContext, authenticate
from albert.services import audit
from albert.trace_feed import TraceFeed, parse_event_id, trace_summary
from albert.trace_writer import get_trace_writer

router = APIRouter(prefix="/v1/console", tags=["console"])
GRAPH_NODE_CAP = 5000
_graph_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _operator(auth: AuthContext = Depends(authenticate)) -> AuthContext:
    auth.require("console.read")
    return auth


def _opt(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset.

    SQLite returns naive timestamps; a browser would read those as local time
    and the explorer's time slider would drift by the operator's UTC offset.
    """
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


_trace_summary = trace_summary
_feed = TraceFeed(SessionLocal, engine)
KEEPALIVE_SECONDS = 15


def _parse_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw_time, raw_id = cursor.split("|", 1)
        return datetime.fromisoformat(raw_time), UUID(raw_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc


@router.get("/traces")
def list_traces(
    kind: str | None = None,
    status: str | None = None,
    organization_id: UUID | None = None,
    principal_id: UUID | None = None,
    name: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    statement = select(Trace)
    equalities = (
        (Trace.kind, kind),
        (Trace.status, status),
        (Trace.organization_id, organization_id),
        (Trace.principal_id, principal_id),
        (Trace.name, name),
    )
    for column, value in equalities:
        if value is not None:
            statement = statement.where(column == value)
    if since is not None:
        statement = statement.where(Trace.started_at >= since)
    if until is not None:
        statement = statement.where(Trace.started_at <= until)
    if q:
        pattern = f"%{q}%"
        statement = statement.where(
            or_(Trace.name.ilike(pattern), Trace.summary["query"].as_string().ilike(pattern))
        )
    if cursor:
        cursor_time, cursor_id = _parse_cursor(cursor)
        if session.bind is not None and session.bind.dialect.name != "postgresql":
            cursor_time = cursor_time.replace(tzinfo=None)
        statement = statement.where(
            or_(
                Trace.started_at < cursor_time,
                (Trace.started_at == cursor_time) & (Trace.id < cursor_id),
            )
        )
    rows = session.scalars(
        statement.order_by(Trace.started_at.desc(), Trace.id.desc()).limit(limit + 1)
    ).all()
    items = rows[:limit]
    next_cursor = (
        f"{items[-1].started_at.isoformat()}|{items[-1].id}" if len(rows) > limit else None
    )
    return {"items": [_trace_summary(t) for t in items], "next_cursor": next_cursor}


@router.get("/stream")
def stream(request: Request, _auth: AuthContext = Depends(_operator)) -> StreamingResponse:
    """Server-sent events: one `trace` event per recorded trace, newest last."""
    after = parse_event_id(request.headers.get("last-event-id"))
    stop = threading.Event()

    def body() -> Iterator[str]:
        last_keepalive = time.monotonic()
        try:
            for event in _feed.events(after, stop):
                if event is None:
                    if time.monotonic() - last_keepalive >= KEEPALIVE_SECONDS:
                        last_keepalive = time.monotonic()
                        yield ": keepalive\n\n"
                    continue
                payload = json.dumps(event["data"])
                yield f"id: {event['id']}\nevent: trace\ndata: {payload}\n\n"
        finally:
            stop.set()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/traces/{trace_id}")
def get_trace(
    trace_id: UUID,
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    trace = session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {**_trace_summary(trace), "spans": trace.spans}


def _edge(relationship: Relationship) -> dict[str, Any]:
    return {
        "id": str(relationship.id),
        "source": str(relationship.source_entity_id),
        "target": str(relationship.target_entity_id),
        "relation_type": relationship.relation_type,
        "sensitivity": relationship.sensitivity,
        "valid_from": _iso(relationship.valid_from),
        "valid_until": _iso(relationship.valid_until),
        "source_memory_id": _opt(relationship.source_memory_id),
    }


@router.get("/graph")
def graph_snapshot(
    organization_id: UUID,
    workspace_id: UUID | None = None,
    temporal_as_of: datetime | None = None,
    limit: int = Query(default=2000, ge=1, le=GRAPH_NODE_CAP),
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Entities, active memories and the edges among them, capped and cached.

    Memory nodes are labelled by type only; subjects and content stay behind the
    audited detail endpoint.
    """
    key = f"{organization_id}|{workspace_id}|{temporal_as_of}|{limit}"
    ttl = get_settings().console_graph_cache_seconds
    cached = _graph_cache.get(key)
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    when = temporal_as_of or datetime.now(UTC)

    entity_scope = [Entity.organization_id == organization_id]
    memory_scope = [Memory.organization_id == organization_id, Memory.status == "active"]
    if workspace_id is not None:
        entity_scope.append(Entity.workspace_id == workspace_id)
        memory_scope.append(Memory.workspace_id == workspace_id)

    entities = session.scalars(
        select(Entity).where(*entity_scope).order_by(Entity.created_at.desc()).limit(limit + 1)
    ).all()
    truncated = len(entities) > limit
    entities = entities[:limit]
    nodes: list[dict[str, Any]] = [
        {
            "id": str(e.id),
            "kind": "entity",
            "label": e.display_name,
            "type": e.entity_type,
            "sensitivity": None,
            "workspace_id": _opt(e.workspace_id),
        }
        for e in entities
    ]
    edges: list[dict[str, Any]] = []
    entity_ids = {e.id for e in entities}
    if entity_ids:
        relationships = session.scalars(
            select(Relationship).where(
                Relationship.organization_id == organization_id,
                Relationship.source_entity_id.in_(entity_ids),
                Relationship.target_entity_id.in_(entity_ids),
                Relationship.valid_from <= when,
                or_(Relationship.valid_until.is_(None), Relationship.valid_until > when),
            )
        ).all()
        edges = [_edge(r) for r in relationships]

    remaining = limit - len(nodes)
    if remaining > 0:
        memories = session.scalars(
            select(Memory)
            .where(*memory_scope)
            .order_by(Memory.created_at.desc())
            .limit(remaining + 1)
        ).all()
        truncated = truncated or len(memories) > remaining
        for memory in memories[:remaining]:
            nodes.append(
                {
                    "id": str(memory.id),
                    "kind": "memory",
                    "label": memory.memory_type,
                    "type": memory.memory_type,
                    "sensitivity": memory.sensitivity,
                    "workspace_id": _opt(memory.workspace_id),
                }
            )
        present = {node["id"] for node in nodes}
        for edge in list(edges):
            memory_id = edge["source_memory_id"]
            if memory_id and memory_id in present:
                edges.append(
                    {
                        **edge,
                        "id": f"derived:{edge['id']}",
                        "source": memory_id,
                        "target": edge["source"],
                        "relation_type": "derived_from",
                        "source_memory_id": None,
                    }
                )
    else:
        truncated = True
    result = {"nodes": nodes, "edges": edges, "truncated": truncated}
    _graph_cache[key] = (time.monotonic(), result)
    return result


@router.get("/overview")
def overview(
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=59)
    buckets = {
        (start + timedelta(minutes=i)).isoformat(): {"count": 0, "errors": 0, "degraded": 0}
        for i in range(60)
    }
    query_start = (
        start.replace(tzinfo=None)
        if session.bind is not None and session.bind.dialect.name != "postgresql"
        else start
    )
    for trace in session.scalars(select(Trace).where(Trace.started_at >= query_start)):
        started = trace.started_at
        started = started.replace(tzinfo=UTC) if started.tzinfo is None else started.astimezone(UTC)
        key = started.replace(second=0, microsecond=0).isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["count"] += 1
        if trace.status == "error":
            bucket["errors"] += 1
        elif trace.status == "degraded":
            bucket["degraded"] += 1
    jobs = session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    oldest = session.scalar(select(func.min(Job.available_at)).where(Job.status == "pending"))
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    last_job = session.scalar(select(func.max(Trace.started_at)).where(Trace.kind == "job"))
    return {
        "traces_per_minute": [{"minute": k, **v} for k, v in buckets.items()],
        "jobs": {status: int(count) for status, count in jobs},
        "oldest_pending_seconds": (
            (datetime.now(UTC) - oldest).total_seconds() if oldest is not None else None
        ),
        "trace_count": int(session.scalar(select(func.count(Trace.id))) or 0),
        "traces_dropped": get_trace_writer().dropped,
        "worker_last_seen": _iso(last_job),
    }


@router.get("/memories/{memory_id}")
def operator_memory(
    memory_id: UUID,
    auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    memory = session.get(Memory, memory_id)
    if memory is None or memory.status == "deleted":
        raise HTTPException(status_code=404, detail="Memory not found")
    audit(
        session,
        auth,
        "memory.read",
        resource_type="memory",
        resource_id=str(memory.id),
        detail={"console": True},
    )
    session.commit()
    return {
        "id": str(memory.id),
        "subject": memory.subject,
        "content": memory.content[:500],
        "memory_type": memory.memory_type,
        "sensitivity": memory.sensitivity,
        "workspace_id": _opt(memory.workspace_id),
        "organization_id": str(memory.organization_id),
        "valid_from": _iso(memory.valid_from),
        "valid_until": _iso(memory.valid_until),
        "embedding_model": memory.embedding_model,
        "metadata": memory.metadata_,
    }


@router.get("/entities/{entity_id}")
def operator_entity(
    entity_id: UUID,
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {
        "id": str(entity.id),
        "name": entity.display_name,
        "entity_type": entity.entity_type,
        "description": entity.description[:500],
        "confidence": entity.confidence,
        "organization_id": str(entity.organization_id),
        "workspace_id": _opt(entity.workspace_id),
    }

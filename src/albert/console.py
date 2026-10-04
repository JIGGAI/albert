"""Operator read API for the memory console.

Everything here is read-only and operator-scoped: it crosses tenants on purpose
and is gated by the console.read capability. Memory content is returned only by
the explicit detail endpoints, audited (memory detail lives in console_map).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.db import SessionLocal, engine, get_session
from albert.graph_store import StoredRelationship, get_graph_store
from albert.models import Job, Memory, Organization, Trace
from albert.security import AuthContext, authenticate
from albert.services import audit
from albert.trace_feed import TraceFeed, parse_event_id, trace_summary
from albert.trace_writer import get_trace_writer

router = APIRouter(prefix="/v1/console", tags=["console"])
GRAPH_NODE_CAP = 5000
GRAPH_CACHE_ENTRIES = 32
_graph_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_get(key: str) -> dict[str, Any] | None:
    ttl = get_settings().console_graph_cache_seconds
    cached = _graph_cache.get(key)
    if cached is None or time.monotonic() - cached[0] >= ttl:
        return None
    return cached[1]


def _cache_put(key: str, value: dict[str, Any]) -> None:
    """Bounded: drop expired entries, then the oldest, before inserting."""
    ttl = get_settings().console_graph_cache_seconds
    now = time.monotonic()
    for stale in [k for k, (stamp, _) in _graph_cache.items() if now - stamp >= ttl]:
        del _graph_cache[stale]
    while len(_graph_cache) >= GRAPH_CACHE_ENTRIES:
        oldest = min(_graph_cache, key=lambda k: _graph_cache[k][0])
        del _graph_cache[oldest]
    _graph_cache[key] = (now, value)


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


_trace_summary = trace_summary  # list/detail endpoints share the feed's shape
_feed = TraceFeed(SessionLocal, engine, get_settings().database_url)
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
def stream(
    request: Request,
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Server-sent events: one `trace` event per recorded trace, newest last.

    The request's database session is released before streaming starts so a
    long-lived stream never pins a pooled connection, and the body is async so
    idle waits do not occupy a threadpool slot.
    """
    after = parse_event_id(request.headers.get("last-event-id"))
    session.close()

    async def body() -> AsyncIterator[str]:
        last_keepalive = time.monotonic()
        async for event in _feed.events(after):
            if event is None:
                if time.monotonic() - last_keepalive >= KEEPALIVE_SECONDS:
                    last_keepalive = time.monotonic()
                    yield ": keepalive\n\n"
                continue
            payload = json.dumps(event["data"])
            yield f"id: {event['id']}\nevent: trace\ndata: {payload}\n\n"

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


def _edge(relationship: StoredRelationship) -> dict[str, Any]:
    return {
        "id": str(relationship.id),
        "source": str(relationship.source.id),
        "target": str(relationship.target.id),
        "relation_type": relationship.relation_type,
        "sensitivity": relationship.sensitivity,
        "valid_from": _iso(relationship.valid_from),
        "valid_until": _iso(relationship.valid_until),
        "source_memory_id": _opt(relationship.source_memory_id),
    }


def _graph_version(session: Session, organization_id: UUID) -> str:
    """A cheap fingerprint of everything the graph snapshot is built from."""
    count, latest = session.execute(
        select(func.count(Memory.id), func.max(Memory.updated_at)).where(
            Memory.organization_id == organization_id
        )
    ).one()
    graph = get_graph_store().fingerprint(session, organization_id=organization_id)
    return f"{count}:{latest}|{graph}"


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
    # The tenant's data version is part of the key, so a cached snapshot is only
    # reused while nothing in that tenant has changed. Without it, a tenant viewed
    # while empty kept showing empty for the whole TTL after its first memory.
    version = _graph_version(session, organization_id)
    key = f"{organization_id}|{workspace_id}|{temporal_as_of}|{limit}|{version}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    when = temporal_as_of or datetime.now(UTC)

    memory_scope = [Memory.organization_id == organization_id, Memory.status == "active"]
    if workspace_id is not None:
        memory_scope.append(Memory.workspace_id == workspace_id)

    entities, relationships, truncated = get_graph_store().snapshot(
        session,
        organization_id=organization_id,
        workspace_id=workspace_id,
        when=when,
        limit=limit,
    )
    nodes: list[dict[str, Any]] = [
        {
            "id": str(e.id),
            "kind": "entity",
            "label": e.name,
            "type": e.entity_type,
            "sensitivity": None,
            "workspace_id": _opt(e.workspace_id),
        }
        for e in entities
    ]
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
    _cache_put(key, result)
    return result


@router.get("/organizations")
def list_organizations(
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Every tenant by name with how much it holds, for the console's tenant picker."""

    def counts(model: Any, *conditions: Any) -> dict[UUID, int]:
        rows = session.execute(
            select(model.organization_id, func.count(model.id))
            .where(*conditions)
            .group_by(model.organization_id)
        )
        return {organization_id: int(count) for organization_id, count in rows}

    memories = counts(Memory, Memory.status == "active")
    graph = get_graph_store().counts(session, when=datetime.now(UTC))
    items = [
        {
            "id": str(organization.id),
            "name": organization.name,
            "memories": memories.get(organization.id, 0),
            "entities": graph.get(organization.id, (0, 0))[0],
            "relationships": graph.get(organization.id, (0, 0))[1],
        }
        for organization in session.scalars(select(Organization).order_by(Organization.name))
    ]
    return {"items": items}


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
    recent = session.execute(
        select(Trace.started_at, Trace.status).where(Trace.started_at >= query_start)
    )
    for started_at, trace_status in recent:
        started = started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at
        key = started.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["count"] += 1
        if trace_status == "error":
            bucket["errors"] += 1
        elif trace_status == "degraded":
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


@router.get("/entities/{entity_id}")
def operator_entity(
    entity_id: UUID,
    auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    entity = get_graph_store().get_entity(session, entity_id=entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    audit(
        session,
        auth,
        "entity.read",
        resource_type="entity",
        resource_id=str(entity.id),
        detail={"console": True},
    )
    session.commit()
    return {
        "id": str(entity.id),
        "name": entity.name,
        "entity_type": entity.entity_type,
        "description": entity.description[:500],
        "confidence": entity.confidence,
        "organization_id": str(entity.organization_id),
        "workspace_id": _opt(entity.workspace_id),
    }

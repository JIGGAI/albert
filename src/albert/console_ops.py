"""Operator views over what agents are doing, what memory holds and how well
retrieval and the service itself are working.

Everything is computed on request from traces, jobs and the derived link and
recall tables; nothing here writes except one audit event when memory titles
are returned.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.console import _iso, _operator, _opt
from albert.console_map import clean_title
from albert.db import get_session
from albert.graph_store import get_graph_store
from albert.links import COUNTED_TRACE_NAMES
from albert.models import (
    Job,
    Memory,
    MemoryLink,
    MemoryStat,
    Principal,
    Trace,
    Workspace,
)
from albert.security import AuthContext
from albert.services import audit
from albert.trace_writer import get_trace_writer

router = APIRouter(prefix="/v1/console/ops", tags=["console"])
STORE_TRACE_NAMES = ("POST /v1/memories", "POST /v1/episodes")
BACKENDS = ("lexical", "vector", "graph")
TRACE_ROW_CAP = 20_000
SPAN_TRACE_CAP = 3_000
NEAR_DUPLICATE = 0.97
LIST_LIMIT = 10
Hours = Query(default=24, ge=1, le=24 * 30)


def percentile(values: list[float], percent: float) -> float | None:
    """Linear-interpolated percentile; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _bound(session: Session, value: datetime) -> datetime:
    """SQLite stores naive timestamps; compare like with like."""
    if session.bind is not None and session.bind.dialect.name != "postgresql":
        return value.replace(tzinfo=None)
    return value


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _traces(
    session: Session,
    *columns: Any,
    since: datetime,
    organization_id: UUID | None,
    names: tuple[str, ...] | None = None,
    cap: int = TRACE_ROW_CAP,
) -> tuple[list[Any], bool]:
    """Newest trace rows in the window, capped; (rows, truncated)."""
    statement = select(*columns).where(Trace.started_at >= _bound(session, since))
    if organization_id is not None:
        statement = statement.where(Trace.organization_id == organization_id)
    if names is not None:
        statement = statement.where(Trace.name.in_(names))
    rows = session.execute(statement.order_by(Trace.started_at.desc()).limit(cap + 1)).all()
    return rows[:cap], len(rows) > cap


@router.get("/agents")
def agent_activity(
    organization_id: UUID | None = None,
    hours: int = Hours,
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Who is using memory: searches, stores and errors per principal, and over time."""
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    rows, truncated = _traces(
        session,
        Trace.principal_id,
        Trace.name,
        Trace.status,
        Trace.started_at,
        Trace.summary,
        since=since,
        organization_id=organization_id,
    )
    bucket_hours = 1 if hours <= 48 else 24
    bucket_count = math.ceil(hours / bucket_hours)
    start = now - timedelta(hours=bucket_hours * bucket_count)
    series = [
        {
            "bucket": (start + timedelta(hours=bucket_hours * index)).isoformat(),
            "recalls": 0,
            "stores": 0,
            "errors": 0,
        }
        for index in range(bucket_count)
    ]
    per: dict[UUID, dict[str, Any]] = defaultdict(
        lambda: {
            "requests": 0,
            "searches": 0,
            "stores": 0,
            "errors": 0,
            "zero_hit_searches": 0,
            "hits": 0,
            "last_seen": None,
        }
    )
    for principal_id, name, status, started_at, summary in rows:
        started = _aware(started_at)
        is_search = name in COUNTED_TRACE_NAMES
        is_store = name in STORE_TRACE_NAMES
        index = int((started - start).total_seconds() // (bucket_hours * 3600))
        if 0 <= index < bucket_count:
            bucket = series[index]
            bucket["recalls"] += int(is_search)
            bucket["stores"] += int(is_store)
            bucket["errors"] += int(status == "error")
        if principal_id is None:
            continue
        entry = per[principal_id]
        entry["requests"] += 1
        entry["errors"] += int(status == "error")
        if entry["last_seen"] is None or started > entry["last_seen"]:
            entry["last_seen"] = started
        if is_store and status != "error":
            entry["stores"] += 1
        if is_search and status != "error":
            hits = int((summary or {}).get("hits") or 0)
            entry["searches"] += 1
            entry["hits"] += hits
            entry["zero_hit_searches"] += int(hits == 0)
    known = (
        {
            principal.id: principal
            for principal in session.scalars(select(Principal).where(Principal.id.in_(set(per))))
        }
        if per
        else {}
    )
    principals = []
    for principal_id, entry in per.items():
        principal = known.get(principal_id)
        principals.append(
            {
                "id": str(principal_id),
                "name": principal.name if principal is not None else "removed principal",
                "type": principal.principal_type if principal is not None else None,
                "organization_id": _opt(principal.organization_id) if principal else None,
                "requests": entry["requests"],
                "searches": entry["searches"],
                "stores": entry["stores"],
                "errors": entry["errors"],
                "zero_hit_searches": entry["zero_hit_searches"],
                "avg_hits": round(entry["hits"] / entry["searches"], 2) if entry["searches"] else 0,
                "last_seen": _iso(entry["last_seen"]),
            }
        )
    principals.sort(key=lambda item: (-item["requests"], item["name"]))
    return {
        "window_hours": hours,
        "bucket_hours": bucket_hours,
        "principals": principals,
        "series": series,
        "truncated": truncated,
    }


@router.get("/memory")
def memory_health(
    organization_id: UUID,
    workspace_id: UUID | None = None,
    auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """What a tenant's memory holds and where it needs attention."""
    scope = [Memory.organization_id == organization_id, Memory.status == "active"]
    if workspace_id is not None:
        scope.append(Memory.workspace_id == workspace_id)

    def grouped(column: Any) -> list[dict[str, Any]]:
        rows = session.execute(
            select(column, func.count(Memory.id)).where(*scope).group_by(column)
        ).all()
        return sorted(
            ({"label": str(label), "count": int(count)} for label, count in rows),
            key=lambda item: (-item["count"], item["label"]),
        )

    total = int(session.scalar(select(func.count(Memory.id)).where(*scope)) or 0)
    workspace_names = dict(
        session.execute(
            select(Workspace.id, Workspace.name).where(Workspace.organization_id == organization_id)
        ).all()
    )
    by_workspace = sorted(
        (
            {"id": _opt(ws), "name": workspace_names.get(ws, "no workspace"), "count": int(count)}
            for ws, count in session.execute(
                select(Memory.workspace_id, func.count(Memory.id))
                .where(*scope)
                .group_by(Memory.workspace_id)
            )
        ),
        key=lambda item: (-item["count"], item["name"]),
    )
    recalled = select(MemoryStat.memory_id).where(MemoryStat.recall_count > 0)
    never_recalled = int(
        session.scalar(select(func.count(Memory.id)).where(*scope, Memory.id.not_in(recalled))) or 0
    )
    unindexed = int(
        session.scalar(
            select(func.count(Memory.id)).where(*scope, Memory.embedding_model.is_(None))
        )
        or 0
    )
    linked = select(MemoryLink.source_memory_id).union(select(MemoryLink.target_memory_id))
    unlinked = int(
        session.scalar(select(func.count(Memory.id)).where(*scope, Memory.id.not_in(linked))) or 0
    )

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    first_day = today - timedelta(days=29)
    per_day: Counter[str] = Counter()
    for (created_at,) in session.execute(
        select(Memory.created_at).where(*scope, Memory.created_at >= _bound(session, first_day))
    ):
        per_day[_aware(created_at).astimezone(UTC).date().isoformat()] += 1
    added = [
        {
            "day": (first_day + timedelta(days=offset)).date().isoformat(),
            "count": per_day.get((first_day + timedelta(days=offset)).date().isoformat(), 0),
        }
        for offset in range(30)
    ]

    top_rows = session.execute(
        select(Memory.id, Memory.subject, func.substr(Memory.content, 1, 400), MemoryStat)
        .join(MemoryStat, MemoryStat.memory_id == Memory.id)
        .where(*scope, MemoryStat.recall_count > 0)
        .order_by(MemoryStat.recall_count.desc(), Memory.id)
        .limit(LIST_LIMIT)
    ).all()
    top_recalled = [
        {
            "id": str(memory_id),
            "title": clean_title(subject, head),
            "recalls": stat.recall_count,
            "last": _iso(stat.last_recalled_at),
        }
        for memory_id, subject, head, stat in top_rows
    ]

    link_scope = [
        MemoryLink.organization_id == organization_id,
        MemoryLink.kind == "similar",
        MemoryLink.weight >= NEAR_DUPLICATE,
    ]
    if workspace_id is not None:
        link_scope.append(MemoryLink.workspace_id == workspace_id)
    pairs = session.execute(
        select(MemoryLink.source_memory_id, MemoryLink.target_memory_id, MemoryLink.weight)
        .where(*link_scope)
        .order_by(MemoryLink.weight.desc(), MemoryLink.id)
        .limit(LIST_LIMIT)
    ).all()
    pair_ids = {memory_id for source, target, _weight in pairs for memory_id in (source, target)}
    titles = (
        {
            memory_id: clean_title(subject, head)
            for memory_id, subject, head in session.execute(
                select(Memory.id, Memory.subject, func.substr(Memory.content, 1, 400)).where(
                    Memory.id.in_(pair_ids), Memory.status == "active"
                )
            )
        }
        if pair_ids
        else {}
    )
    near_duplicates = [
        {
            "a": {"id": str(source), "title": titles[source]},
            "b": {"id": str(target), "title": titles[target]},
            "similarity": round(float(weight), 4),
        }
        for source, target, weight in pairs
        if source in titles and target in titles
    ]
    near_duplicate_total = int(
        session.scalar(select(func.count(MemoryLink.id)).where(*link_scope)) or 0
    )

    audit(
        session,
        auth,
        "console.ops.memory.read",
        resource_type="organization",
        resource_id=str(organization_id),
        detail={"console": True, "workspace_id": _opt(workspace_id)},
    )
    session.commit()
    return {
        "total": total,
        "by_type": grouped(Memory.memory_type),
        "by_sensitivity": grouped(Memory.sensitivity),
        "by_workspace": by_workspace,
        "never_recalled": never_recalled,
        "unindexed": unindexed,
        "unlinked": unlinked,
        "near_duplicate_pairs": near_duplicate_total,
        "added": added,
        "top_recalled": top_recalled,
        "near_duplicates": near_duplicates,
    }


@router.get("/service")
def service_health(
    hours: int = Hours,
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """How the service itself is doing: endpoints, the job queue, the recorder."""
    now = datetime.now(UTC)
    rows, truncated = _traces(
        session,
        Trace.kind,
        Trace.name,
        Trace.status,
        Trace.duration_ms,
        since=now - timedelta(hours=hours),
        organization_id=None,
    )
    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"durations": [], "errors": 0, "degraded": 0}
    )
    requests = request_errors = job_runs = 0
    for kind, name, status, duration_ms in rows:
        entry = grouped[(kind, name)]
        entry["durations"].append(float(duration_ms))
        entry["errors"] += int(status == "error")
        entry["degraded"] += int(status == "degraded")
        if kind == "request":
            requests += 1
            request_errors += int(status == "error")
        else:
            job_runs += 1
    endpoints = sorted(
        (
            {
                "kind": kind,
                "name": name,
                "count": len(entry["durations"]),
                "errors": entry["errors"],
                "degraded": entry["degraded"],
                "p50_ms": percentile(entry["durations"], 50),
                "p95_ms": percentile(entry["durations"], 95),
            }
            for (kind, name), entry in grouped.items()
        ),
        key=lambda item: (-item["count"], item["name"]),
    )

    by_status = {
        status: int(count)
        for status, count in session.execute(
            select(Job.status, func.count(Job.id)).group_by(Job.status)
        )
    }
    oldest = session.scalar(select(func.min(Job.available_at)).where(Job.status == "pending"))
    failures = session.scalars(
        select(Job)
        .where(or_(Job.status == "failed", Job.last_error.is_not(None)))
        .order_by(Job.updated_at.desc())
        .limit(LIST_LIMIT)
    ).all()
    last_job = session.scalar(select(func.max(Trace.started_at)).where(Trace.kind == "job"))
    database_bytes = None
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        database_bytes = int(
            session.scalar(text("SELECT pg_database_size(current_database())")) or 0
        )
    settings = get_settings()
    return {
        "window_hours": hours,
        "endpoints": endpoints,
        "totals": {
            "requests": requests,
            "errors": request_errors,
            "error_rate": _rate(request_errors, requests),
            "job_runs": job_runs,
        },
        "jobs": {
            "by_status": by_status,
            "oldest_pending_seconds": (
                round((now - _aware(oldest)).total_seconds(), 1) if oldest is not None else None
            ),
            "recent_failures": [
                {
                    "id": str(job.id),
                    "job_type": job.job_type,
                    "status": job.status,
                    "attempts": job.attempts,
                    # The message can embed memory text; only its type is shown.
                    "error": (job.last_error or "").split(":", 1)[0][:100] or None,
                    "at": _iso(job.updated_at),
                }
                for job in failures
            ],
        },
        "traces": {
            "stored": int(session.scalar(select(func.count(Trace.id))) or 0),
            "dropped": get_trace_writer().dropped,
            "retention_days": settings.trace_retention_days,
            "sample_rate": settings.trace_sample_rate,
        },
        "worker_last_seen": _iso(last_job),
        "database_bytes": database_bytes,
        "config": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "classifier": settings.graph_builder,
            "classification_model": settings.classification_model,
            "graph_store": get_graph_store().name,
        },
        "truncated": truncated,
    }


@router.get("/retrieval")
def retrieval_quality(
    organization_id: UUID | None = None,
    hours: int = Hours,
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Whether searches find things, how fast, and which backend earns the hits."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows, truncated = _traces(
        session,
        Trace.id,
        Trace.status,
        Trace.duration_ms,
        Trace.started_at,
        Trace.summary,
        Trace.spans,
        since=since,
        organization_id=organization_id,
        names=COUNTED_TRACE_NAMES,
        cap=SPAN_TRACE_CAP,
    )
    durations: list[float] = []
    hit_total = zero_hit = degraded = errors = final_hits = 0
    found_by: Counter[str] = Counter()
    found_only_by: Counter[str] = Counter()
    backend_durations: dict[str, list[float]] = defaultdict(list)
    backend_failures: Counter[str] = Counter()
    empty_queries: dict[str, dict[str, Any]] = {}
    slowest: list[dict[str, Any]] = []
    for trace_id, status, duration_ms, started_at, summary, spans in rows:
        summary = summary or {}
        if status == "error":
            errors += 1
            continue
        hits = int(summary.get("hits") or 0)
        query = str(summary.get("query") or "")
        durations.append(float(duration_ms))
        hit_total += hits
        degraded += int(status == "degraded")
        slowest.append(
            {
                "trace_id": str(trace_id),
                "query": query,
                "duration_ms": round(float(duration_ms), 2),
                "hits": hits,
                "at": _iso(started_at),
            }
        )
        if hits == 0:
            zero_hit += 1
            entry = empty_queries.setdefault(
                query, {"query": query, "count": 0, "last": started_at}
            )
            entry["count"] += 1
            entry["last"] = max(entry["last"], started_at)
        for span in spans or []:
            name = span.get("name")
            if name in BACKENDS:
                backend_durations[name].append(float(span.get("duration_ms") or 0))
                backend_failures[name] += int(span.get("status") == "error")
            elif name == "fuse":
                for hit in (span.get("detail") or {}).get("hits") or []:
                    backends = [b for b in hit.get("backends") or [] if b in BACKENDS]
                    final_hits += 1
                    found_by.update(set(backends))
                    if len(set(backends)) == 1:
                        found_only_by[backends[0]] += 1
    searches = len(durations)
    slowest.sort(key=lambda item: -item["duration_ms"])
    zero_hit_queries = sorted(
        ({**entry, "last": _iso(entry["last"])} for entry in empty_queries.values()),
        key=lambda item: (-item["count"], item["query"]),
    )
    return {
        "window_hours": hours,
        "searches": searches,
        "errors": errors,
        "zero_hit": zero_hit,
        "zero_hit_rate": _rate(zero_hit, searches),
        "degraded": degraded,
        "degraded_rate": _rate(degraded, searches),
        "avg_hits": round(hit_total / searches, 2) if searches else 0,
        "p50_ms": percentile(durations, 50),
        "p95_ms": percentile(durations, 95),
        "backends": [
            {
                "name": backend,
                # Of the hits agents received, how many this backend found at all,
                # and how many only it found (what would be lost without it).
                "hit_share": _rate(found_by[backend], final_hits),
                "solo_share": _rate(found_only_by[backend], final_hits),
                "p50_ms": percentile(backend_durations[backend], 50),
                "p95_ms": percentile(backend_durations[backend], 95),
                "failures": backend_failures[backend],
            }
            for backend in BACKENDS
        ],
        "zero_hit_queries": zero_hit_queries[:LIST_LIMIT],
        "slowest": slowest[:LIST_LIMIT],
        "truncated": truncated,
    }

"""Operator view of the knowledge-graph backends: what is active, whether it is
reachable, how much it holds and how it has been performing.

Read-only. It reports what is configured and the steps to change it; switching
a backend is a deployment change, never something this API does. Connection
settings can hold credentials, so none of them are returned.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.console import _operator
from albert.console_ops import _aware, _traces, percentile
from albert.db import get_session
from albert.graph_store import GRAPH_BUILDERS, GRAPH_STORES, BackendOption, get_graph_store
from albert.links import COUNTED_TRACE_NAMES
from albert.models import Memory, MemoryChunk, Trace
from albert.security import AuthContext

router = APIRouter(prefix="/v1/console", tags=["console"])
INDEXING_JOBS = ("enrich_memory", "enrich_episode")


def _option(identifier: str, option: BackendOption, *, active: bool) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": option.label,
        "summary": option.summary,
        "choose_when": option.choose_when,
        "available": option.available,
        "active": active,
        "enable": list(option.enable),
    }


def _probe(session: Session) -> tuple[dict[str, Any], dict[str, int] | None]:
    """Reach the active store once: version, round-trip time and how much it holds."""
    store = get_graph_store()
    started = time.perf_counter()
    try:
        version = store.ping(session)
        totals = store.counts(session, when=datetime.now(UTC))
    except Exception as exc:
        # The message can carry a connection string; the type is enough to act on.
        return (
            {"reachable": False, "version": None, "latency_ms": None, "error": type(exc).__name__},
            None,
        )
    latency = round((time.perf_counter() - started) * 1000, 2)
    return (
        {"reachable": True, "version": version, "latency_ms": latency, "error": None},
        {
            "entities": sum(entities for entities, _edges in totals.values()),
            "edges": sum(edges for _entities, edges in totals.values()),
        },
    )


@router.get("/backends")
def backends(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings()
    active_store = get_graph_store().name
    status, counts = _probe(session)
    stores = []
    for identifier, option in GRAPH_STORES.items():
        entry = _option(identifier, option, active=identifier == active_store)
        entry["status"] = status if entry["active"] else None
        entry["counts"] = counts if entry["active"] else None
        stores.append(entry)
    builders = []
    for identifier, option in GRAPH_BUILDERS.items():
        entry = _option(identifier, option, active=identifier == settings.graph_builder)
        entry["model"] = (
            settings.classification_model
            if entry["active"] and identifier == "llm-classifier"
            else None
        )
        builders.append(entry)

    total = int(session.scalar(select(func.count(Memory.id)).where(Memory.status == "active")) or 0)
    embedded = int(
        session.scalar(
            select(func.count(Memory.id)).where(
                Memory.status == "active", Memory.embedding_model.is_not(None)
            )
        )
        or 0
    )
    vectors = {
        "provider": settings.embedding_provider,
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
        "chunks": int(session.scalar(select(func.count(MemoryChunk.id))) or 0),
        "memories_embedded": embedded,
        "memories_total": total,
    }

    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    bucket_hours = 1 if hours <= 48 else 24
    bucket_count = -(-hours // bucket_hours)
    start = now - timedelta(hours=bucket_hours * bucket_count)
    series = [
        {"bucket": (start + timedelta(hours=bucket_hours * i)).isoformat(), "edges_written": 0}
        for i in range(bucket_count)
    ]
    jobs, jobs_truncated = _traces(
        session,
        Trace.status,
        Trace.started_at,
        Trace.spans,
        since=since,
        organization_id=None,
        names=INDEXING_JOBS,
        cap=5000,
    )
    writes: list[float] = []
    classifies: list[float] = []
    totals: Counter[str] = Counter()
    for job_status, started_at, spans in jobs:
        totals["jobs"] += 1
        totals["job_failures"] += int(job_status == "error")
        for span in spans or []:
            detail = span.get("detail") or {}
            if span.get("name") == "classify":
                classifies.append(float(span.get("duration_ms") or 0))
                totals["extraction_failures"] += int(span.get("status") == "error")
            elif span.get("name") == "write_edges":
                written = int(detail.get("edges_written") or 0)
                writes.append(float(span.get("duration_ms") or 0))
                totals["edges_written"] += written
                totals["edges_closed"] += int(detail.get("edges_closed") or 0)
                totals["write_failures"] += int(span.get("status") == "error")
                index = int((_aware(started_at) - start).total_seconds() // (bucket_hours * 3600))
                if 0 <= index < bucket_count:
                    series[index]["edges_written"] += written
    searches, searches_truncated = _traces(
        session,
        Trace.spans,
        since=since,
        organization_id=None,
        names=(*COUNTED_TRACE_NAMES, "POST /v1/graph/query"),
        cap=3000,
    )
    queries: list[float] = []
    for (spans,) in searches:
        for span in spans or []:
            if span.get("name") == "graph":
                queries.append(float(span.get("duration_ms") or 0))
                totals["graph_failures"] += int(span.get("status") == "error")
                totals["graph_candidates"] += int(
                    (span.get("detail") or {}).get("candidates_total") or 0
                )
    return {
        "stores": stores,
        "builders": builders,
        "vectors": vectors,
        "activity": {
            "window_hours": hours,
            "bucket_hours": bucket_hours,
            "indexing_jobs": totals["jobs"],
            "indexing_failures": totals["job_failures"],
            "extraction_failures": totals["extraction_failures"],
            "edges_written": totals["edges_written"],
            "edges_closed": totals["edges_closed"],
            "write_failures": totals["write_failures"],
            "write_p50_ms": percentile(writes, 50),
            "write_p95_ms": percentile(writes, 95),
            "classify_p50_ms": percentile(classifies, 50),
            "classify_p95_ms": percentile(classifies, 95),
            "graph_queries": len(queries),
            "graph_candidates": totals["graph_candidates"],
            "graph_failures": totals["graph_failures"],
            "graph_query_p50_ms": percentile(queries, 50),
            "graph_query_p95_ms": percentile(queries, 95),
            "series": series,
            "truncated": jobs_truncated or searches_truncated,
        },
    }

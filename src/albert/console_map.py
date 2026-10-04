"""Operator API for the console's memory map.

The map carries memory titles and the detail endpoint full content, so every
content read and every search here is audited. Traces still hold ids only.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, defer

from albert.clusters import cluster_nodes, name_clusters
from albert.config import get_settings
from albert.console import _cache_get, _cache_put, _iso, _operator, _opt
from albert.db import get_session
from albert.models import (
    Episode,
    Memory,
    MemoryChunk,
    MemoryLink,
    MemoryRecall,
    MemoryStat,
    Organization,
    Workspace,
)
from albert.recorder import current_recorder
from albert.schemas import SearchRequest
from albert.search import hybrid_search
from albert.security import AuthContext
from albert.services import audit

router = APIRouter(prefix="/v1/console", tags=["console"])
MAP_NODE_CAP = 3000
TITLE_LIMIT = 120
TITLE_SOURCE_CHARACTERS = 400
CONTENT_LIMIT = 50_000
RELATED_LIMIT = 40
RECENT_RECALLS = 10
RETRIEVAL_STEPS = ("lexical", "vector", "graph", "fuse")
ALL_SENSITIVITIES = ["public", "internal", "confidential", "restricted"]


def clean_title(subject: str | None, content: str | None) -> str:
    """A display title: the subject without markdown heading marks, else the first line."""
    for source in (subject or "", *(content or "").splitlines()):
        title = source.strip().lstrip("#").strip()
        if not any(character.isalnum() for character in title):
            continue  # blank, or a front-matter rule such as "---"
        if "_" in title and not any(character.isspace() for character in title):
            title = title.replace("_", " ")  # a file slug reads better as words
        return title[:TITLE_LIMIT]
    return "Untitled"


EpisodeSource = tuple[dict[str, Any] | None, str]  # (extra, source_uri)


def _source(memory: Memory, episode: EpisodeSource | None) -> dict[str, Any]:
    """Team, role and source of a memory from its own fields and its episode's."""
    extra: dict[str, Any] = {**((episode[0] if episode is not None else None) or {})}
    extra.update(memory.metadata_ or {})
    task_ref = memory.task_ref or ""
    role = extra.get("role") or (task_ref[5:] if task_ref.startswith("role:") else None)
    path = extra.get("path") or extra.get("file")
    return {
        "team": str(extra.get("team") or memory.project_ref or "") or None,
        "role": str(role) if role else None,
        "source_uri": (episode[1] if episode is not None else "") or None,
        "path": str(path) if path else None,
    }


def _episodes(session: Session, episode_ids: set[UUID]) -> dict[UUID, EpisodeSource]:
    """Source fields of episodes, without loading their (possibly very large) content."""
    if not episode_ids:
        return {}
    rows = session.execute(
        select(Episode.id, Episode.extra, Episode.source_uri).where(Episode.id.in_(episode_ids))
    )
    return {episode_id: (extra, source_uri) for episode_id, extra, source_uri in rows}


def _map_version(session: Session, organization_id: UUID) -> str:
    """A cheap fingerprint of everything a map snapshot is built from."""
    parts = []
    for model, key in (
        (Memory, Memory.id),
        (MemoryLink, MemoryLink.id),
        (MemoryStat, MemoryStat.memory_id),
    ):
        count, latest = session.execute(
            select(func.count(key), func.max(model.updated_at)).where(
                model.organization_id == organization_id
            )
        ).one()
        parts.append(f"{count}:{latest}")
    return "|".join(parts)


def _require_organization(session: Session, organization_id: UUID) -> None:
    if session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="Organization not found")


@router.get("/workspaces")
def list_workspaces(
    organization_id: UUID,
    _auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """A tenant's workspaces with how many active memories each holds."""
    counts = dict(
        session.execute(
            select(Memory.workspace_id, func.count(Memory.id))
            .where(Memory.organization_id == organization_id, Memory.status == "active")
            .group_by(Memory.workspace_id)
        ).all()
    )
    items = [
        {
            "id": str(workspace.id),
            "name": workspace.name,
            "memories": int(counts.get(workspace.id, 0)),
        }
        for workspace in session.scalars(
            select(Workspace)
            .where(Workspace.organization_id == organization_id)
            .order_by(Workspace.name)
        )
    ]
    return {"items": items, "unscoped_memories": int(counts.get(None, 0))}


@router.get("/map")
def memory_map(
    organization_id: UUID,
    workspace_id: UUID | None = None,
    limit: int | None = Query(default=None, ge=1, le=MAP_NODE_CAP),
    auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Titled memory nodes, the links among them and named clusters; newest first."""
    limit = limit or get_settings().map_node_limit
    audit(
        session,
        auth,
        "console.map.read",
        resource_type="organization",
        resource_id=str(organization_id),
        detail={"console": True, "workspace_id": _opt(workspace_id)},
    )
    session.commit()
    version = _map_version(session, organization_id)
    key = f"map|{organization_id}|{workspace_id}|{limit}|{version}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    scope = [Memory.organization_id == organization_id, Memory.status == "active"]
    if workspace_id is not None:
        scope.append(Memory.workspace_id == workspace_id)
    # A title needs the subject or the first line, never the whole body.
    rows = session.execute(
        select(Memory, func.substr(Memory.content, 1, TITLE_SOURCE_CHARACTERS))
        .options(defer(Memory.content))
        .where(*scope)
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .limit(limit + 1)
    ).all()
    truncated = len(rows) > limit
    rows = rows[:limit]
    memories = [memory for memory, _head in rows]
    heads = {memory.id: head for memory, head in rows}
    present = {memory.id for memory in memories}
    episodes = _episodes(session, {m.episode_id for m in memories if m.episode_id is not None})
    stats = (
        {
            stat.memory_id: stat
            for stat in session.scalars(select(MemoryStat).where(MemoryStat.memory_id.in_(present)))
        }
        if memories
        else {}
    )

    link_rows = (
        session.execute(
            select(
                MemoryLink.source_memory_id,
                MemoryLink.target_memory_id,
                MemoryLink.kind,
                MemoryLink.weight,
            ).where(
                MemoryLink.organization_id == organization_id,
                MemoryLink.source_memory_id.in_(present),
                MemoryLink.target_memory_id.in_(present),
            )
        ).all()
        if memories
        else []
    )
    links = [
        {"source": str(source), "target": str(target), "kind": kind, "weight": float(weight)}
        for source, target, kind, weight in link_rows
    ]
    links.sort(key=lambda link: (link["kind"], link["source"], link["target"]))

    titles = {str(memory.id): clean_title(memory.subject, heads[memory.id]) for memory in memories}
    assignment = cluster_nodes(
        list(titles),
        [
            (link["source"], link["target"], link["weight"])
            for link in links
            if link["kind"] == "similar"
        ],
    )
    names = name_clusters(titles, assignment)
    sizes: dict[int, int] = {}
    for cluster in assignment.values():
        sizes[cluster] = sizes.get(cluster, 0) + 1

    nodes = []
    for memory in memories:
        node_id = str(memory.id)
        stat = stats.get(memory.id)
        source = _source(memory, episodes.get(memory.episode_id))  # type: ignore[arg-type]
        nodes.append(
            {
                "id": node_id,
                "title": titles[node_id],
                "type": memory.memory_type,
                "team": source["team"],
                "role": source["role"],
                "sensitivity": memory.sensitivity,
                "workspace_id": _opt(memory.workspace_id),
                "recalls": stat.recall_count if stat is not None else 0,
                "last_recalled_at": _iso(stat.last_recalled_at) if stat is not None else None,
                "created_at": _iso(memory.created_at),
                "cluster": assignment.get(node_id),
            }
        )
    result = {
        "nodes": nodes,
        "links": links,
        "clusters": [
            {"id": cluster, "label": names[cluster], "size": sizes[cluster]}
            for cluster in sorted(sizes)
        ],
        "truncated": truncated,
    }
    _cache_put(key, result)
    return result


@router.get("/memories/{memory_id}")
def operator_memory(
    memory_id: UUID,
    auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """One memory in full for the reading pane, with what it connects to."""
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
    episode = (
        _episodes(session, {memory.episode_id}).get(memory.episode_id)
        if memory.episode_id is not None
        else None
    )

    link_rows = session.scalars(
        select(MemoryLink).where(
            or_(MemoryLink.source_memory_id == memory.id, MemoryLink.target_memory_id == memory.id)
        )
    ).all()
    other_ids = {
        link.target_memory_id if link.source_memory_id == memory.id else link.source_memory_id
        for link in link_rows
    }
    others = (
        {
            other.id: other
            for other in session.scalars(
                select(Memory).where(Memory.id.in_(other_ids), Memory.status == "active")
            )
        }
        if other_ids
        else {}
    )
    related = []
    for link in link_rows:
        outgoing = link.source_memory_id == memory.id
        other = others.get(link.target_memory_id if outgoing else link.source_memory_id)
        if other is None:
            continue
        related.append(
            {
                "id": str(other.id),
                "title": clean_title(other.subject, other.content),
                "type": other.memory_type,
                "kind": link.kind,
                "weight": float(link.weight),
                "direction": (
                    ("next" if outgoing else "previous") if link.kind == "sequence" else None
                ),
            }
        )
    kind_order = {"sequence": 0, "similar": 1, "recalled": 2}
    related.sort(key=lambda item: (kind_order.get(item["kind"], 9), -item["weight"], item["id"]))

    stat = session.get(MemoryStat, memory.id)
    recent = session.scalars(
        select(MemoryRecall)
        .where(MemoryRecall.memory_id == memory.id)
        .order_by(MemoryRecall.recalled_at.desc())
        .limit(RECENT_RECALLS)
    ).all()
    chunk_count = session.scalar(
        select(func.count(MemoryChunk.id)).where(MemoryChunk.memory_id == memory.id)
    )
    return {
        "id": str(memory.id),
        "title": clean_title(memory.subject, memory.content),
        "subject": memory.subject,
        "content": memory.content[:CONTENT_LIMIT],
        "truncated": len(memory.content) > CONTENT_LIMIT,
        "memory_type": memory.memory_type,
        "sensitivity": memory.sensitivity,
        "workspace_id": _opt(memory.workspace_id),
        "organization_id": str(memory.organization_id),
        "created_at": _iso(memory.created_at),
        "valid_from": _iso(memory.valid_from),
        "valid_until": _iso(memory.valid_until),
        "embedding_model": memory.embedding_model,
        "chunk_count": int(chunk_count or 0),
        **_source(memory, episode),
        "related": related[:RELATED_LIMIT],
        "recalls": {
            "count": stat.recall_count if stat is not None else 0,
            "last": _iso(stat.last_recalled_at) if stat is not None else None,
            "recent": [
                {
                    "trace_id": str(recall.trace_id),
                    "at": _iso(recall.recalled_at),
                    "query": recall.query,
                    "rank": recall.rank,
                    "principal_id": _opt(recall.principal_id),
                }
                for recall in recent
            ],
        },
    }


class ConsoleSearch(BaseModel):
    organization_id: UUID
    workspace_id: UUID | None = None
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=20, ge=1, le=50)
    # The map searches memories only; the retrieval probe adds the graph leg so
    # an operator sees what an agent's search would see.
    include_graph: bool = False


@router.post("/search")
def console_search(
    data: ConsoleSearch,
    auth: AuthContext = Depends(_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Operator search across every sensitivity in one tenant scope.

    Recorded under its own trace name, so it never counts toward recall statistics.
    Alongside the hits it returns the path the request took: each retrieval step
    with its timing and how many candidates it produced, and the trace id for
    the full replay.
    """
    started = time.perf_counter()
    _require_organization(session, data.organization_id)
    request = SearchRequest(
        query=data.query,
        limit=data.limit,
        sensitivity=ALL_SENSITIVITIES,
        include_graph=data.include_graph,
        # Relationship hits are titled from their sentence; memory bodies are
        # never returned from here either way.
        include_content=data.include_graph,
    )
    result = hybrid_search(
        session,
        organization_id=data.organization_id,
        workspace_id=data.workspace_id,
        request=request,
        allowed_sensitivities=ALL_SENSITIVITIES,
    )
    ids = [hit.memory_id for hit in result.hits if hit.memory_id is not None]
    memories = (
        {m.id: m for m in session.scalars(select(Memory).where(Memory.id.in_(ids)))} if ids else {}
    )
    hits: list[dict[str, Any]] = []
    for hit in result.hits:
        memory = memories.get(hit.memory_id) if hit.memory_id is not None else None
        if hit.kind == "relationship":
            hits.append(
                {
                    "id": str(hit.metadata.get("relationship_id") or ""),
                    "kind": "relationship",
                    "memory_id": str(memory.id) if memory is not None else None,
                    "rank": len(hits) + 1,
                    "title": hit.content[:TITLE_LIMIT],
                    "type": hit.subject,
                    "sensitivity": str(hit.metadata.get("sensitivity") or ""),
                    "score": hit.score,
                    "backends": hit.backends,
                }
            )
            continue
        if memory is None:
            continue
        hits.append(
            {
                "id": str(memory.id),
                "kind": "memory",
                "memory_id": str(memory.id),
                "rank": len(hits) + 1,
                "title": clean_title(memory.subject, memory.content),
                "type": memory.memory_type,
                "sensitivity": memory.sensitivity,
                "score": hit.score,
                "backends": hit.backends,
            }
        )
    recorder = current_recorder()
    path = [
        {
            "name": step["name"],
            "status": step["status"],
            "started_offset_ms": step["started_offset_ms"],
            "duration_ms": step["duration_ms"],
            "candidates": (
                len(hits)
                if step["name"] == "fuse"
                else int((step.get("detail") or {}).get("candidates_total") or 0)
            ),
            "error": (step.get("detail") or {}).get("error"),
        }
        for step in (recorder.spans if recorder is not None else [])
        if step["name"] in RETRIEVAL_STEPS
    ]
    audit(
        session,
        auth,
        "memory.searched",
        resource_type="organization",
        resource_id=str(data.organization_id),
        detail={"console": True, "result_count": len(hits), "degraded": result.degraded},
    )
    session.commit()
    return {
        "hits": hits,
        "degraded": result.degraded,
        "path": path,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "trace_id": str(recorder.id) if recorder is not None else None,
    }

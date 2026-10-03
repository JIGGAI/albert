from __future__ import annotations

import argparse
import os
from contextvars import ContextVar
from typing import Any

import httpx
import uvicorn
from mcp.server import MCPServer
from starlette.responses import JSONResponse

from albert import __version__

mcp = MCPServer(
    "Albert",
    description="Independent hybrid memory service for AI agents",
    version=__version__,
)

_request_api_key: ContextVar[str | None] = ContextVar("albert_mcp_request_api_key", default=None)


class ForwardBearerAuth:
    """Require an Albert bearer key and make it available to the REST adapter."""

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, value = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not value:
            response = JSONResponse(
                {"detail": "Bearer API key required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        token = _request_api_key.set(value)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_api_key.reset(token)


def _settings() -> tuple[str, str]:
    url = os.environ.get("ALBERT_API_URL", "http://127.0.0.1:8080").rstrip("/")
    key = _request_api_key.get() or os.environ.get("ALBERT_MCP_API_KEY", "")
    if not key:
        raise RuntimeError(
            "An HTTP Authorization bearer key or ALBERT_MCP_API_KEY for stdio is required"
        )
    return url, key


async def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base_url, api_key = _settings()
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.request(
            method,
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Albert API returned {response.status_code}: {response.text[:2000]}")
    return None if response.status_code == 204 else response.json()


@mcp.tool()
async def memory_remember(
    content: str,
    subject: str = "",
    memory_type: str = "fact",
    project_ref: str | None = None,
    task_ref: str | None = None,
    sensitivity: str = "internal",
) -> dict[str, Any]:
    """Store an explicit durable memory and queue semantic/graph enrichment."""
    return await _request(
        "POST",
        "/v1/memories",
        {
            "content": content,
            "subject": subject,
            "memory_type": memory_type,
            "project_ref": project_ref,
            "task_ref": task_ref,
            "sensitivity": sensitivity,
        },
    )


@mcp.tool()
async def memory_ingest(
    content: str,
    source_type: str = "agent",
    source_uri: str = "",
    project_ref: str | None = None,
    task_ref: str | None = None,
    sensitivity: str = "internal",
) -> dict[str, Any]:
    """Ingest a canonical episode for asynchronous classification and indexing."""
    return await _request(
        "POST",
        "/v1/episodes",
        {
            "content": content,
            "source_type": source_type,
            "source_uri": source_uri,
            "project_ref": project_ref,
            "task_ref": task_ref,
            "sensitivity": sensitivity,
        },
    )


@mcp.tool()
async def memory_get(memory_id: str) -> dict[str, Any]:
    """Read one authorized memory by UUID."""
    return await _request("GET", f"/v1/memories/{memory_id}")


@mcp.tool()
async def memory_update(
    memory_id: str,
    subject: str | None = None,
    content: str | None = None,
    memory_type: str | None = None,
    sensitivity: str | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """Update an authorized durable memory and re-enrich changed content."""
    payload = {
        key: value
        for key, value in {
            "subject": subject,
            "content": content,
            "memory_type": memory_type,
            "sensitivity": sensitivity,
            "confidence": confidence,
        }.items()
        if value is not None
    }
    return await _request("PATCH", f"/v1/memories/{memory_id}", payload)


@mcp.tool()
async def memory_get_episode(episode_id: str) -> dict[str, Any]:
    """Read one authorized canonical episode by UUID."""
    return await _request("GET", f"/v1/episodes/{episode_id}")


@mcp.tool()
async def memory_search(
    query: str,
    limit: int = 10,
    project_ref: str | None = None,
    task_ref: str | None = None,
    include_graph: bool = True,
) -> dict[str, Any]:
    """Hybrid lexical, vector, and temporal-graph memory retrieval."""
    return await _request(
        "POST",
        "/v1/search",
        {
            "query": query,
            "limit": limit,
            "project_ref": project_ref,
            "task_ref": task_ref,
            "include_graph": include_graph,
        },
    )


@mcp.tool()
async def memory_assemble_context(
    query: str,
    limit: int = 10,
    max_characters: int = 12000,
    project_ref: str | None = None,
    task_ref: str | None = None,
) -> dict[str, Any]:
    """Build a bounded, cited context block using Albert's hybrid retrieval."""
    return await _request(
        "POST",
        "/v1/context/assemble",
        {
            "query": query,
            "limit": limit,
            "max_characters": max_characters,
            "project_ref": project_ref,
            "task_ref": task_ref,
        },
    )


@mcp.tool()
async def memory_forget(memory_id: str) -> dict[str, bool]:
    """Delete an authorized memory and invalidate its derived relationships."""
    await _request("DELETE", f"/v1/memories/{memory_id}")
    return {"deleted": True}


@mcp.tool()
async def memory_query_graph(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Search temporally valid entity relationships."""
    return await _request("POST", "/v1/graph/query", {"query": query, "limit": limit})


@mcp.tool()
async def memory_get_subgraph(
    entity_id: str, hops: int = 2, limit: int = 50
) -> list[dict[str, Any]]:
    """Traverse the authorized temporal subgraph around an entity."""
    return await _request(
        "GET", f"/v1/graph/entities/{entity_id}/subgraph?hops={hops}&limit={limit}"
    )


@mcp.tool()
async def memory_add_relationship(
    source_name: str,
    relation_type: str,
    target_name: str,
    source_type: str = "concept",
    target_type: str = "concept",
    sensitivity: str = "internal",
    source_memory_id: str | None = None,
) -> dict[str, Any]:
    """Create an explicit typed relationship in the authorized memory graph."""
    return await _request(
        "POST",
        "/v1/relationships",
        {
            "source": {"name": source_name, "entity_type": source_type},
            "relation_type": relation_type,
            "target": {"name": target_name, "entity_type": target_type},
            "sensitivity": sensitivity,
            "source_memory_id": source_memory_id,
        },
    )


@mcp.tool()
async def working_memory_start(
    task_id: str,
    description: str,
    resource_type: str | None = None,
    resource_ref: str | None = None,
    expires_in_seconds: int = 7200,
    acquire_lock: bool = False,
) -> dict[str, Any]:
    """Start mutable task coordination state."""
    return await _request(
        "POST",
        "/v1/working-memory",
        {
            "task_id": task_id,
            "description": description,
            "resource_type": resource_type,
            "resource_ref": resource_ref,
            "expires_in_seconds": expires_in_seconds,
            "acquire_lock": acquire_lock,
        },
    )


@mcp.tool()
async def memory_export(workspace_id: str | None = None) -> dict[str, Any]:
    """Export authorized durable memories and explicit graph relationships."""
    suffix = f"?workspace_id={workspace_id}" if workspace_id else ""
    return await _request("GET", f"/v1/export{suffix}")


@mcp.tool()
async def memory_import(bundle: dict[str, Any], workspace_id: str | None = None) -> dict[str, Any]:
    """Import an Albert export bundle, optionally flattening it into one workspace."""
    return await _request("POST", "/v1/import", {"bundle": bundle, "workspace_id": workspace_id})


@mcp.tool()
async def working_memory_update(
    working_id: str, progress: dict[str, Any], expires_in_seconds: int | None = None
) -> dict[str, Any]:
    """Update task progress and optionally extend its expiration."""
    return await _request(
        "PATCH",
        f"/v1/working-memory/{working_id}",
        {"progress": progress, "expires_in_seconds": expires_in_seconds},
    )


@mcp.tool()
async def working_memory_get(working_id: str) -> dict[str, Any]:
    """Read current task coordination state by UUID."""
    return await _request("GET", f"/v1/working-memory/{working_id}")


@mcp.tool()
async def working_memory_complete(working_id: str, consolidation_notes: str = "") -> dict[str, Any]:
    """Complete task coordination state and release associated locks."""
    return await _request(
        "POST",
        f"/v1/working-memory/{working_id}/complete",
        {"consolidation_notes": consolidation_notes},
    )


@mcp.tool()
async def working_memory_fail(working_id: str, consolidation_notes: str = "") -> dict[str, Any]:
    """Mark task coordination state failed and release associated locks."""
    return await _request(
        "POST",
        f"/v1/working-memory/{working_id}/fail",
        {"consolidation_notes": consolidation_notes},
    )


@mcp.tool()
async def memory_lock_acquire(
    resource_type: str,
    resource_ref: str,
    working_memory_id: str | None = None,
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    """Acquire a fenced, expiring exclusive resource lease."""
    return await _request(
        "POST",
        "/v1/locks/acquire",
        {
            "resource_type": resource_type,
            "resource_ref": resource_ref,
            "working_memory_id": working_memory_id,
            "ttl_seconds": ttl_seconds,
        },
    )


@mcp.tool()
async def memory_lock_renew(
    lock_id: str, token: str, fence: int, ttl_seconds: int = 900
) -> dict[str, Any]:
    """Renew a resource lease if its secret token and fencing value are current."""
    return await _request(
        "POST",
        f"/v1/locks/{lock_id}/renew",
        {"token": token, "fence": fence, "ttl_seconds": ttl_seconds},
    )


@mcp.tool()
async def memory_lock_release(lock_id: str, token: str, fence: int) -> dict[str, bool]:
    """Release a resource lease."""
    await _request("POST", f"/v1/locks/{lock_id}/release", {"token": token, "fence": fence})
    return {"released": True}


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        if args.transport == "sse":
            app = mcp.sse_app(host=args.host)
        else:
            app = mcp.streamable_http_app(host=args.host)
        uvicorn.run(ForwardBearerAuth(app), host=args.host, port=args.port)


if __name__ == "__main__":
    run()

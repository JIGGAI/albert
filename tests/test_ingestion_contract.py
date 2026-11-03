"""Ingestion contract v1: how `POST /v1/episodes` identifies, stores and forgets a source.

External connectors (including private ones) build against exactly these
behaviors, so each test pins one of them. See "Ingestion contract (v1)" in
docs/API_AND_MCP.md. Graph forgetting on delete is covered by
tests/test_graph_forget.py and tests/test_graphiti_forget.py.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from albert.db import SessionLocal
from albert.models import Workspace
from albert.schemas import MAX_JSON_FIELD_CHARACTERS

from .conftest import create_identity, create_principal_key, drain_jobs
from .test_shared_memories import _client

pytestmark = pytest.mark.anyio

WRITE_ONLY = ["memory.write"]
CLEANUP = ["memory.read", "memory.delete"]
READ_ONLY = ["memory.read"]


def _tenant():  # type: ignore[no-untyped-def]
    """An organization with a Default and a Docs workspace, as a connector deployment sees it."""
    _admin, organization, default = create_identity("Ingestion tenant", workspace_scoped=False)
    with SessionLocal() as session:
        docs = Workspace(organization_id=organization.id, name="Docs")
        session.add(docs)
        session.commit()
        session.refresh(docs)
    writer = create_principal_key(organization, None, "Connector writer", WRITE_ONLY)
    cleanup = create_principal_key(organization, None, "Connector cleanup", CLEANUP)
    reader = create_principal_key(organization, None, "Reader", READ_ONLY)
    return organization, default, docs, writer, cleanup, reader


def _body(workspace: Workspace, **overrides: object) -> dict:
    token = uuid4().hex[:8]
    body = {
        "content": f"Document: handbook-{token}.pdf\n\nThe Pelican{token} rollout starts Monday.",
        "metadata": {
            "contentHash": f"sha256-{token}",
            "filename": f"handbook-{token}.pdf",
            "locator": {"page": 3},
            "sourceId": f"source-{token}",
            "sourceRevisionId": f"revision-{token}",
        },
        "source_type": "albert-cloud-document",
        "source_uri": f"albert-cloud://documents/job-{token}/part-1",
        "workspace_id": str(workspace.id),
    }
    body.update(overrides)
    return body


async def _post(key: str, body: dict) -> httpx.Response:
    async with _client(key) as client:
        return await client.post("/v1/episodes", json=body)


async def _search_ids(key: str, query: str) -> set[str]:
    async with _client(key) as client:
        response = await client.post("/v1/search", json={"query": query, "limit": 20})
    assert response.status_code == 200, response.text
    return {hit["memory_id"] for hit in response.json()["hits"]}


async def test_identical_repost_returns_the_same_episode_with_current_indexing() -> None:
    _org, _default, docs, writer, _cleanup, reader = _tenant()
    body = _body(docs)

    first = await _post(writer, body)
    assert first.status_code == 201, first.text
    episode = first.json()
    assert episode["indexing"]["state"] == "queued"
    assert episode["indexing"]["searchable"] is False

    again = await _post(writer, body)
    assert again.status_code == 200
    assert again.json()["id"] == episode["id"]
    assert again.json()["indexing"]["state"] == "queued"
    assert again.headers["location"] == f"/v1/episodes/{episode['id']}"
    assert "retry-after" in again.headers

    drain_jobs()
    indexed = await _post(writer, body)
    assert indexed.status_code == 200
    assert indexed.json()["id"] == episode["id"]
    indexing = indexed.json()["indexing"]
    assert indexing["state"] == "complete" and indexing["searchable"] is True
    assert indexing["memory_id"]
    assert "retry-after" not in indexed.headers
    assert indexing["memory_id"] in await _search_ids(reader, body["content"].split()[-4])


async def test_dedup_is_per_principal() -> None:
    organization, _default, docs, writer, _cleanup, _reader = _tenant()
    body = _body(docs)
    other_writer = create_principal_key(organization, None, "Second writer", WRITE_ONLY)
    first = (await _post(writer, body)).json()
    other = await _post(other_writer, body)
    assert other.status_code == 201
    assert other.json()["id"] != first["id"]


async def test_changed_content_at_the_same_source_uri_is_a_new_episode() -> None:
    _org, _default, docs, writer, _cleanup, reader = _tenant()
    body = _body(docs)
    first = (await _post(writer, body)).json()
    revised = await _post(writer, {**body, "content": body["content"] + " Revised."})
    assert revised.status_code == 201
    assert revised.json()["id"] != first["id"]

    # No upsert by source: the earlier revision is still there, untouched.
    async with _client(reader) as client:
        old = await client.get(f"/v1/episodes/{first['id']}")
    assert old.status_code == 200
    assert old.json()["content"] == body["content"]
    assert old.json()["deleted_at"] is None


async def test_same_content_at_a_different_source_uri_is_a_new_episode() -> None:
    _org, _default, docs, writer, _cleanup, _reader = _tenant()
    body = _body(docs)
    first = (await _post(writer, body)).json()
    moved = await _post(writer, {**body, "source_uri": body["source_uri"] + "-retry"})
    assert moved.status_code == 201
    assert moved.json()["id"] != first["id"]


async def test_same_content_and_source_in_another_workspace_is_a_distinct_episode() -> None:
    _org, default, docs, writer, _cleanup, _reader = _tenant()
    body = _body(docs)
    in_docs = (await _post(writer, body)).json()
    in_default = await _post(writer, {**body, "workspace_id": str(default.id)})
    assert in_default.status_code == 201
    assert in_default.json()["id"] != in_docs["id"]
    assert in_docs["workspace_id"] == str(docs.id)
    assert in_default.json()["workspace_id"] == str(default.id)


async def test_source_fields_and_metadata_round_trip() -> None:
    _org, _default, docs, writer, _cleanup, reader = _tenant()
    body = _body(docs)
    created = (await _post(writer, body)).json()
    async with _client(reader) as client:
        read = await client.get(f"/v1/episodes/{created['id']}")
    assert read.status_code == 200
    episode = read.json()
    assert episode["source_type"] == "albert-cloud-document"
    assert episode["source_uri"] == body["source_uri"]
    assert episode["workspace_id"] == str(docs.id)
    assert episode["content"] == body["content"]
    assert len(episode["content_hash"]) == 64  # sha256 of content, not metadata.contentHash
    # Metadata comes back as `extra`, verbatim.
    assert episode["extra"] == body["metadata"]
    assert "metadata" not in episode


async def test_oversized_metadata_and_long_source_fields_are_rejected() -> None:
    _org, _default, docs, writer, _cleanup, _reader = _tenant()
    oversized = {"blob": "x" * MAX_JSON_FIELD_CHARACTERS}
    assert (await _post(writer, _body(docs, metadata=oversized))).status_code == 422
    assert (await _post(writer, _body(docs, source_uri="u" * 1001))).status_code == 422
    assert (await _post(writer, _body(docs, source_type="t" * 65))).status_code == 422


async def test_write_only_key_can_create_but_not_read_or_search() -> None:
    _org, _default, docs, writer, _cleanup, _reader = _tenant()
    created = await _post(writer, _body(docs))
    assert created.status_code == 201
    async with _client(writer) as client:
        read = await client.get(f"/v1/episodes/{created.json()['id']}")
        search = await client.post("/v1/search", json={"query": "Pelican"})
        delete = await client.delete(f"/v1/episodes/{created.json()['id']}")
    assert read.status_code == 403
    assert read.json()["detail"] == "Missing capability: memory.read"
    assert search.status_code == 403
    assert delete.status_code == 403


async def test_writing_into_a_deleted_or_unknown_workspace_is_refused_with_404() -> None:
    organization, _default, docs, writer, _cleanup, _reader = _tenant()
    unknown = await _post(writer, {**_body(docs), "workspace_id": str(uuid4())})
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Workspace not found"}

    manager = create_principal_key(organization, None, "Manager", ["keys.manage"])
    async with _client(manager) as client:
        gone = await client.delete(
            f"/v1/settings/workspaces/{docs.id}", params={"memories": "organization"}
        )
    assert gone.status_code == 200, gone.text
    refused = await _post(writer, _body(docs))
    assert refused.status_code == 404
    assert refused.json() == {"detail": "Workspace not found"}

    # Another organization's workspace is indistinguishable from a missing one.
    _, _other_org, foreign = create_identity("Other tenant", workspace_scoped=False)
    foreign_write = await _post(writer, _body(foreign))
    assert foreign_write.status_code == 404
    assert foreign_write.json() == {"detail": "Workspace not found"}


async def test_organization_key_targets_a_workspace_and_workspace_key_cannot_leave_its_own() -> (
    None
):
    organization, default, docs, writer, _cleanup, _reader = _tenant()
    targeted = await _post(writer, _body(docs))
    assert targeted.status_code == 201
    assert targeted.json()["workspace_id"] == str(docs.id)

    # Without workspace_id an organization-wide key writes organization-wide.
    unscoped = _body(docs)
    del unscoped["workspace_id"]
    organization_wide = await _post(writer, unscoped)
    assert organization_wide.status_code == 201
    assert organization_wide.json()["workspace_id"] is None

    docs_writer = create_principal_key(organization, docs, "Docs writer", WRITE_ONLY)
    elsewhere = await _post(docs_writer, _body(default))
    assert elsewhere.status_code == 403
    assert elsewhere.json()["detail"] == "Workspace is outside the API key scope"
    own = await _post(docs_writer, _body(docs))
    assert own.status_code == 201


async def test_delete_forgets_derived_memories_and_is_404_once_gone() -> None:
    organization, _default, docs, writer, cleanup, reader = _tenant()
    body = _body(docs)
    created = (await _post(writer, body)).json()
    drain_jobs()
    memory_id = (await _post(writer, body)).json()["indexing"]["memory_id"]
    term = body["content"].split()[-4]
    assert memory_id in await _search_ids(reader, term)

    async with _client(reader) as client:
        no_delete = await client.delete(f"/v1/episodes/{created['id']}")
    assert no_delete.status_code == 403
    delete_only = create_principal_key(organization, None, "Delete only", ["memory.delete"])
    async with _client(delete_only) as client:
        blind = await client.delete(f"/v1/episodes/{created['id']}")
    assert blind.status_code == 403
    assert blind.json()["detail"] == "Missing capability: memory.read"

    # A different principal with memory.read + memory.delete may delete it.
    async with _client(cleanup) as client:
        deleted = await client.delete(f"/v1/episodes/{created['id']}")
        assert deleted.status_code == 204
        assert deleted.content == b""
        again = await client.delete(f"/v1/episodes/{created['id']}")
        assert again.status_code == 404
        assert again.json() == {"detail": "Episode not found"}
        never = await client.delete(f"/v1/episodes/{uuid4()}")
        assert never.status_code == 404
        read = await client.get(f"/v1/episodes/{created['id']}")
        assert read.status_code == 404
        memory = await client.get(f"/v1/memories/{memory_id}")
        assert memory.status_code == 404

    assert memory_id not in await _search_ids(reader, term)


async def test_identical_repost_after_delete_returns_the_deleted_episode() -> None:
    """Pinned as-is: dedup ignores deletion, so the tombstone comes back.

    The episode is emptied (content "", extra {}), carries deleted_at, and its
    indexing still reads complete/searchable with no memory_id. Nothing is
    re-indexed. A connector that wants the content back must vary source_uri.
    """
    _org, _default, docs, writer, cleanup, reader = _tenant()
    body = _body(docs)
    created = (await _post(writer, body)).json()
    drain_jobs()
    async with _client(cleanup) as client:
        assert (await client.delete(f"/v1/episodes/{created['id']}")).status_code == 204

    reposted = await _post(writer, body)
    assert reposted.status_code == 200
    tombstone = reposted.json()
    assert tombstone["id"] == created["id"]
    assert tombstone["deleted_at"] is not None
    assert tombstone["content"] == ""
    assert tombstone["extra"] == {}
    assert tombstone["indexing"]["state"] == "complete"
    assert tombstone["indexing"]["searchable"] is True
    assert tombstone["indexing"]["memory_id"] is None
    drain_jobs()
    assert await _search_ids(reader, body["content"].split()[-4]) == set()

    fresh = await _post(writer, {**body, "source_uri": body["source_uri"] + "?job=2"})
    assert fresh.status_code == 201
    assert fresh.json()["id"] != created["id"]
    assert fresh.json()["deleted_at"] is None

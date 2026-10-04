from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import or_, select

from albert.api import app
from albert.db import SessionLocal
from albert.models import Memory, MemoryLink, Trace, Workspace
from albert.trace_writer import get_trace_writer

from .conftest import create_identity, drain_jobs

pytestmark = pytest.mark.anyio

VOICE = "Brand voice posts sound sharp confident masculine friendly persuasive energetic bold"
VOICE_2 = VOICE + " captions"
UNRELATED = "Kubernetes autoscaler nodepool rollout cordon drain upgrade maintenance window"


async def _remember(client: httpx.AsyncClient, content: str, **extra) -> str:  # type: ignore[no-untyped-def]
    response = await client.post("/v1/memories", json={"content": content, **extra})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _links(memory_id: str, kind: str = "similar") -> list[MemoryLink]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(MemoryLink).where(
                    MemoryLink.kind == kind,
                    or_(
                        MemoryLink.source_memory_id == UUID(memory_id),
                        MemoryLink.target_memory_id == UUID(memory_id),
                    ),
                )
            )
        )


async def test_similar_memories_are_linked_and_unrelated_are_not(
    client: httpx.AsyncClient,
) -> None:
    first = await _remember(client, VOICE)
    second = await _remember(client, VOICE_2)
    other = await _remember(client, UNRELATED)
    drain_jobs()
    links = _links(first)
    assert len(links) == 1
    link = links[0]
    assert {str(link.source_memory_id), str(link.target_memory_id)} == {first, second}
    assert str(link.source_memory_id) < str(link.target_memory_id)
    assert link.weight >= 0.78
    assert _links(other) == []


async def test_reindex_replaces_similar_links(client: httpx.AsyncClient) -> None:
    first = await _remember(client, VOICE)
    await _remember(client, VOICE_2)
    drain_jobs()
    assert len(_links(first)) == 1
    patched = await client.patch(f"/v1/memories/{first}", json={"content": UNRELATED})
    assert patched.status_code == 200
    drain_jobs()
    assert _links(first) == []


async def test_links_stay_inside_a_workspace() -> None:
    key, organization, first_workspace = create_identity("Link Scope", workspace_scoped=False)
    with SessionLocal() as session:
        second_workspace = Workspace(organization_id=organization.id, name="Second")
        session.add(second_workspace)
        session.commit()
        second_id = str(second_workspace.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    ) as org_client:
        here = await _remember(org_client, VOICE, workspace_id=str(first_workspace.id))
        await _remember(org_client, VOICE_2, workspace_id=second_id)
        drain_jobs()
    assert _links(here) == []


async def test_dated_entries_form_a_sequence(client: httpx.AsyncClient) -> None:
    ids = []
    for day, text in (("2026-03-27", "Friday notes about recruiting"), ("2026-03-28", UNRELATED)):
        response = await client.post(
            "/v1/episodes",
            json={
                "content": f"# Daily Memory {day}\n{text}",
                "source_uri": f"openclaw://team/memory/{day}.md",
                "occurred_at": f"{day}T12:00:00+00:00",
            },
        )
        assert response.status_code == 201
        ids.append(response.json()["id"])
    drain_jobs()
    with SessionLocal() as session:
        memories = {
            str(m.episode_id): str(m.id)
            for m in session.scalars(
                select(Memory).where(Memory.episode_id.in_([UUID(i) for i in ids]))
            )
        }
    links = _links(memories[ids[1]], kind="sequence")
    assert len(links) == 1
    assert str(links[0].source_memory_id) == memories[ids[0]]
    assert str(links[0].target_memory_id) == memories[ids[1]]
    # Editing the earlier entry re-indexes it; the chain to the later one must survive.
    edited = await client.patch(
        f"/v1/memories/{memories[ids[0]]}", json={"content": "Friday notes, corrected"}
    )
    assert edited.status_code == 200, edited.text
    drain_jobs()
    assert len(_links(memories[ids[1]], kind="sequence")) == 1


async def test_deleting_a_memory_removes_its_links(client: httpx.AsyncClient) -> None:
    first = await _remember(client, VOICE)
    await _remember(client, VOICE_2)
    drain_jobs()
    assert len(_links(first)) == 1
    assert (await client.delete(f"/v1/memories/{first}")).status_code == 204
    assert _links(first) == []


async def test_link_failure_does_not_fail_indexing(client: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from albert import links

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("link backend unavailable")

    monkeypatch.setattr(links, "nearest_memories", boom)
    memory_id = await _remember(client, VOICE)
    drain_jobs()
    fetched = await client.get(f"/v1/memories/{memory_id}")
    assert fetched.json()["embedding_model"]
    get_trace_writer().flush()
    with SessionLocal() as session:
        trace = session.scalar(
            select(Trace).where(Trace.name == "enrich_memory").order_by(Trace.started_at.desc())
        )
    span = next(s for s in trace.spans if s["name"] == "link")
    assert span["status"] == "ok"
    assert span["detail"]["error"] == "RuntimeError"


async def test_rebuild_links_restores_deleted_links(client: httpx.AsyncClient, identity) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from albert.admin import app as admin_app

    _key, organization, _workspace = identity
    first = await _remember(client, VOICE)
    await _remember(client, VOICE_2)
    drain_jobs()
    with SessionLocal() as session:
        session.query(MemoryLink).filter(MemoryLink.organization_id == organization.id).delete()
        session.commit()
    assert _links(first) == []
    result = CliRunner().invoke(admin_app, ["rebuild-links", "--organization", organization.name])
    assert result.exit_code == 0, result.output
    assert "similar" in result.output
    assert len(_links(first)) == 1

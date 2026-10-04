from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from albert.api import app
from albert.db import SessionLocal
from albert.links import process_recall_traces
from albert.models import AuditEvent, MemoryStat, Workspace
from albert.trace_writer import get_trace_writer

from .conftest import create_identity, create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio

VOICE = "Brand voice posts sound sharp confident masculine friendly persuasive energetic bold"
PAYOUT = "Daily payout export reconciles stylist commission deposits against bank transfers"


def _console(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    )


def _agent(key: str) -> httpx.AsyncClient:
    return _console(key)


@pytest.fixture()
def tenant():  # type: ignore[no-untyped-def]
    """An org-wide agent key, a console key, and the organization."""
    key, organization, workspace = create_identity("Map Agent", workspace_scoped=False)
    console_key = create_principal_key(organization, None, "Console", ["console.read"])
    return key, console_key, organization, workspace


async def _seed_group(
    client: httpx.AsyncClient, subject: str, base: str, count: int, **extra
) -> list[str]:  # type: ignore[no-untyped-def]
    ids = []
    for index in range(count):
        response = await client.post(
            "/v1/memories",
            json={"subject": f"# {subject} {index}", "content": f"{base} variant{index}", **extra},
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return ids


async def test_workspaces_lists_names_and_counts(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, organization, workspace = tenant
    with SessionLocal() as session:
        other = Workspace(organization_id=organization.id, name="Engineering")
        session.add(other)
        session.commit()
        other_id = str(other.id)
    async with _agent(key) as client:
        await _seed_group(client, "Brand voice", VOICE, 2, workspace_id=str(workspace.id))
        await _seed_group(client, "Payout export", PAYOUT, 1, workspace_id=other_id)
    async with _console(console_key) as console:
        response = await console.get(
            "/v1/console/workspaces", params={"organization_id": str(organization.id)}
        )
        assert response.status_code == 200, response.text
        items = {item["name"]: item for item in response.json()["items"]}
        assert items["Default"]["memories"] == 2
        assert items["Engineering"]["memories"] == 1
        assert items["Engineering"]["id"] == other_id


async def test_map_returns_titled_nodes_links_and_clusters(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, organization, _workspace = tenant
    async with _agent(key) as client:
        voice = await _seed_group(client, "Brand voice", VOICE, 4, project_ref="hmx-social-team")
        payout = await _seed_group(client, "Payout export", PAYOUT, 3)
        drain_jobs()
        await client.post("/v1/search", json={"query": "brand voice posts sharp confident"})
    get_trace_writer().flush()
    with SessionLocal() as session:
        process_recall_traces(session, settle_seconds=0)
    async with _console(console_key) as console:
        response = await console.get(
            "/v1/console/map", params={"organization_id": str(organization.id)}
        )
        assert response.status_code == 200, response.text
        body = response.json()
    nodes = {node["id"]: node for node in body["nodes"]}
    assert set(nodes) == set(voice + payout)
    assert body["truncated"] is False
    first = nodes[voice[0]]
    assert first["title"] == "Brand voice 0"  # leading "# " stripped
    assert first["team"] == "hmx-social-team"
    assert first["type"] and first["sensitivity"] == "internal"
    assert first["recalls"] >= 1 and first["last_recalled_at"]
    assert nodes[payout[0]]["recalls"] == 0
    kinds = {link["kind"] for link in body["links"]}
    assert {"similar", "recalled"} <= kinds
    for link in body["links"]:
        assert link["source"] in nodes and link["target"] in nodes
        assert set(link) == {"source", "target", "kind", "weight"}
    clusters = {cluster["id"]: cluster for cluster in body["clusters"]}
    assert [clusters[0]["size"], clusters[1]["size"]] == [4, 3]
    assert clusters[0]["label"] == "Brand Voice"
    assert clusters[1]["label"] == "Payout Export"
    assert {nodes[i]["cluster"] for i in voice} == {0}
    assert {nodes[i]["cluster"] for i in payout} == {1}
    assert "content" not in first and "subject" not in first


async def test_map_is_scoped_to_workspace(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, organization, workspace = tenant
    with SessionLocal() as session:
        other = Workspace(organization_id=organization.id, name="Engineering")
        session.add(other)
        session.commit()
        other_id = str(other.id)
    async with _agent(key) as client:
        mine = await _seed_group(client, "Brand voice", VOICE, 3, workspace_id=str(workspace.id))
        theirs = await _seed_group(client, "Brand voice", VOICE, 3, workspace_id=other_id)
        drain_jobs()
        # An org-wide search recalls memories from both workspaces together.
        await client.post("/v1/search", json={"query": "brand voice posts sharp confident"})
    get_trace_writer().flush()
    with SessionLocal() as session:
        process_recall_traces(session, settle_seconds=0)
    async with _console(console_key) as console:
        scoped = (
            await console.get(
                "/v1/console/map",
                params={"organization_id": str(organization.id), "workspace_id": other_id},
            )
        ).json()
        everything = (
            await console.get("/v1/console/map", params={"organization_id": str(organization.id)})
        ).json()
    ids = {node["id"] for node in scoped["nodes"]}
    assert ids == set(theirs)
    assert scoped["links"]
    for link in scoped["links"]:
        assert link["source"] in ids and link["target"] in ids
    assert {node["id"] for node in everything["nodes"]} == set(mine + theirs)


async def test_map_cap_and_truncated_flag(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, organization, _workspace = tenant
    async with _agent(key) as client:
        ids = await _seed_group(client, "Brand voice", VOICE, 4)
        drain_jobs()
    org = str(organization.id)
    async with _console(console_key) as console:
        capped = await console.get("/v1/console/map", params={"organization_id": org, "limit": 2})
        body = capped.json()
        assert body["truncated"] is True
        assert [node["id"] for node in body["nodes"]] == [ids[3], ids[2]]  # newest first
        present = {node["id"] for node in body["nodes"]}
        assert all(
            link["source"] in present and link["target"] in present for link in body["links"]
        )
        too_big = await console.get(
            "/v1/console/map", params={"organization_id": org, "limit": 3001}
        )
        assert too_big.status_code == 422


async def test_map_cache_follows_new_links(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, organization, _workspace = tenant
    org = str(organization.id)
    async with _agent(key) as client, _console(console_key) as console:
        await _seed_group(client, "Brand voice", VOICE, 3)
        before = (await console.get("/v1/console/map", params={"organization_id": org})).json()
        assert len(before["nodes"]) == 3 and before["links"] == []
        drain_jobs()  # indexing writes links; memory rows themselves may not change
        after = (await console.get("/v1/console/map", params={"organization_id": org})).json()
        assert len(after["links"]) >= 2
        await client.post("/v1/search", json={"query": "brand voice posts sharp confident"})
        get_trace_writer().flush()
        with SessionLocal() as session:
            process_recall_traces(session, settle_seconds=0)
        recalled = (await console.get("/v1/console/map", params={"organization_id": org})).json()
        assert sum(node["recalls"] for node in recalled["nodes"]) >= 3


async def test_memory_detail_returns_full_content_related_and_recalls(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, organization, _workspace = tenant
    long_content = "## Voice rules\n\n" + (VOICE + ". ") * 40
    assert len(long_content) > 2000
    async with _agent(key) as client:
        created = await client.post(
            "/v1/memories", json={"subject": "# Voice rules", "content": long_content}
        )
        memory_id = created.json()["id"]
        others = await _seed_group(client, "Brand voice", VOICE, 2)
        episode = await client.post(
            "/v1/episodes",
            json={
                "source_type": "document",
                "source_uri": f"openclaw://hmx-social-team/notes/{uuid4()}.md",
                "content": "# Episode voice\n" + VOICE,
                "project_ref": "hmx-social-team",
                "metadata": {"team": "hmx-social-team", "role": "lead", "path": "notes/x.md"},
            },
        )
        assert episode.status_code == 201, episode.text
        drain_jobs()
        await client.post("/v1/search", json={"query": "voice rules brand sharp confident"})
    get_trace_writer().flush()
    with SessionLocal() as session:
        process_recall_traces(session, settle_seconds=0)
    async with _console(console_key) as console:
        response = await console.get(f"/v1/console/memories/{memory_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["content"] == long_content and body["truncated"] is False
        assert body["title"] == "Voice rules"
        assert body["chunk_count"] >= 1
        assert "metadata" not in body
        related = {item["id"]: item for item in body["related"]}
        assert set(others) <= set(related)
        assert related[others[0]]["title"].startswith("Brand voice")
        assert related[others[0]]["kind"] in {"similar", "recalled"}
        assert body["recalls"]["count"] >= 1 and body["recalls"]["last"]
        recent = body["recalls"]["recent"][0]
        assert recent["query"] == "voice rules brand sharp confident"
        assert recent["trace_id"] and recent["rank"] >= 1
        full_map = (
            await console.get("/v1/console/map", params={"organization_id": str(organization.id)})
        ).json()
        derived = next(n for n in full_map["nodes"] if n["title"] == "Episode voice")
        assert derived["team"] == "hmx-social-team" and derived["role"] == "lead"
        detail = (await console.get(f"/v1/console/memories/{derived['id']}")).json()
        assert detail["source_uri"].startswith("openclaw://hmx-social-team/notes/")
        assert detail["path"] == "notes/x.md" and detail["role"] == "lead"
    with SessionLocal() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "memory.read", AuditEvent.resource_id == memory_id
            )
        ).all()
        assert events and events[0].detail.get("console") is True


async def test_memory_detail_flags_truncated_content(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, _organization, _workspace = tenant
    async with _agent(key) as client:
        created = await client.post("/v1/memories", json={"content": "word " * 12_000})
    async with _console(console_key) as console:
        body = (await console.get(f"/v1/console/memories/{created.json()['id']}")).json()
        assert len(body["content"]) == 50_000 and body["truncated"] is True


async def test_console_search_ranks_hits_and_is_audited(tenant) -> None:  # type: ignore[no-untyped-def]
    key, console_key, organization, _workspace = tenant
    async with _agent(key) as client:
        voice = await _seed_group(client, "Brand voice", VOICE, 2)
        secret = await client.post(
            "/v1/memories",
            json={"subject": "Voice vault", "content": VOICE, "sensitivity": "confidential"},
        )
        assert secret.status_code == 201, secret.text
        await _seed_group(client, "Payout export", PAYOUT, 2)
        drain_jobs()
    get_trace_writer().flush()
    with SessionLocal() as session:
        process_recall_traces(session, settle_seconds=0)
    async with _console(console_key) as console:
        response = await console.post(
            "/v1/console/search",
            json={
                "organization_id": str(organization.id),
                "query": "brand voice sharp",
                "limit": 5,
            },
        )
        assert response.status_code == 200, response.text
        hits = response.json()["hits"]
        assert [hit["rank"] for hit in hits] == list(range(1, len(hits) + 1))
        top = {hit["id"] for hit in hits[:3]}
        assert set(voice) <= top and secret.json()["id"] in top  # all sensitivities
        assert hits[0]["title"] and hits[0]["backends"] and hits[0]["score"] > 0
        assert "content" not in hits[0]
        missing = await console.post(
            "/v1/console/search", json={"organization_id": str(uuid4()), "query": "x"}
        )
        assert missing.status_code == 404
    get_trace_writer().flush()
    with SessionLocal() as session:
        assert process_recall_traces(session, settle_seconds=0) == 0
        assert session.get(MemoryStat, __import__("uuid").UUID(voice[0])) is None
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "memory.searched")
        ).all()
        assert any((event.detail or {}).get("console") is True for event in events)


async def test_map_endpoints_require_console_capability(tenant) -> None:  # type: ignore[no-untyped-def]
    key, _console_key, organization, _workspace = tenant
    org = str(organization.id)
    async with _agent(key) as client:
        assert (
            await client.get("/v1/console/workspaces", params={"organization_id": org})
        ).status_code == 403
        assert (
            await client.get("/v1/console/map", params={"organization_id": org})
        ).status_code == 403
        assert (
            await client.post("/v1/console/search", json={"organization_id": org, "query": "x"})
        ).status_code == 403

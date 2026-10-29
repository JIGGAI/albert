"""Deleting a workspace shares, moves or forgets its memories, and retires its keys."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from albert.db import SessionLocal
from albert.models import AuditEvent, Entity, Episode, Memory, Principal, Workspace

from .conftest import create_principal_key, drain_jobs
from .test_shared_memories import READ_WRITE, _client, _store, _tenant

pytestmark = pytest.mark.anyio


async def _relate(key: str, source: str, target: str) -> None:
    async with _client(key) as client:
        response = await client.post(
            "/v1/relationships",
            json={
                "source": {"name": source},
                "relation_type": "depends_on",
                "target": {"name": target},
            },
        )
        assert response.status_code == 201, response.text


async def _edges(key: str, query: str) -> set[tuple[str, str]]:
    async with _client(key) as client:
        edges = (await client.post("/v1/graph/query", json={"query": query})).json()
    return {(edge["source_name"], edge["target_name"]) for edge in edges}


async def _delete(key: str, workspace, **params: str) -> httpx.Response:  # type: ignore[no-untyped-def]
    async with _client(key) as client:
        return await client.delete(f"/v1/settings/workspaces/{workspace.id}", params=params)


def _manager(organization, *extra: str) -> str:  # type: ignore[no-untyped-def]
    return create_principal_key(organization, None, "Org manager", ["keys.manage", *extra])


async def test_memories_can_become_organization_wide() -> None:
    _org_key, organization, home, sibling, _home_key, sibling_key = _tenant()
    memory = (await _store(sibling_key, "Sibling runbook: rotate the Okapi cache weekly")).json()
    await _relate(sibling_key, "Okapi Cache", "Okapi Store")
    drain_jobs()
    reader = create_principal_key(organization, home, "Home reader", READ_WRITE)
    async with _client(reader) as client:
        assert (await client.get(f"/v1/memories/{memory['id']}")).status_code == 404

    deleted = await _delete(_manager(organization), sibling, memories="organization")
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["records"]["memories"] == 1 and body["records"]["relationships"] == 1
    assert body["credentials"]["api_keys"] == 1

    async with _client(reader) as client:
        shared = await client.get(f"/v1/memories/{memory['id']}")
        assert shared.status_code == 200 and shared.json()["workspace_id"] is None
        hits = (await client.post("/v1/search", json={"query": "Okapi"})).json()["hits"]
        assert memory["id"] in {hit["memory_id"] for hit in hits}
    assert ("Okapi Cache", "Okapi Store") in await _edges(reader, "okapi")
    # Its key stopped working the moment the workspace went.
    async with _client(sibling_key) as client:
        assert (await client.post("/v1/search", json={"query": "Okapi"})).status_code == 401
    with SessionLocal() as session:
        assert session.get(Workspace, sibling.id) is None
        principal = session.scalar(select(Principal).where(Principal.name == "Sibling agent"))
        assert principal is not None and principal.active is False
        assert principal.workspace_id is None
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "workspace.delete",
                AuditEvent.resource_id == str(sibling.id),
            )
        )
        assert event is not None and event.detail["memories"] == "organization"


async def test_memories_can_move_to_another_workspace_merging_entities() -> None:
    _org_key, organization, home, sibling, home_key, sibling_key = _tenant()
    memory = (await _store(sibling_key, "Sibling note: the Quokka API needs a VPN")).json()
    await _relate(sibling_key, "Quokka API", "Quokka DB")
    # The same entity already exists at home; the move must not make a second node.
    await _relate(home_key, "Quokka Portal", "Quokka API")
    drain_jobs()

    deleted = await _delete(_manager(organization), sibling, memories="workspace", to=str(home.id))
    assert deleted.status_code == 200, deleted.text

    async with _client(home_key) as client:
        moved = await client.get(f"/v1/memories/{memory['id']}")
        assert moved.status_code == 200 and moved.json()["workspace_id"] == str(home.id)
    names = await _edges(home_key, "quokka")
    assert {("Quokka API", "Quokka DB"), ("Quokka Portal", "Quokka API")} <= names
    with SessionLocal() as session:
        nodes = session.scalars(
            select(Entity).where(
                Entity.organization_id == organization.id, Entity.canonical_name == "quokka api"
            )
        ).all()
        assert [node.workspace_id for node in nodes] == [home.id]


async def test_an_identical_episode_already_in_the_target_is_merged() -> None:
    _org_key, organization, home, sibling, _home_key, _sibling_key = _tenant()
    writer = create_principal_key(organization, None, "Org writer", READ_WRITE)
    async with _client(writer) as client:
        for workspace in (home, sibling):
            posted = await client.post(
                "/v1/episodes",
                json={
                    "content": "Standup notes: Wombat release slips a week",
                    "source_uri": "notes://standup",
                    "workspace_id": str(workspace.id),
                },
            )
            assert posted.status_code in (200, 201, 202), posted.text
    drain_jobs()
    deleted = await _delete(_manager(organization), sibling, memories="workspace", to=str(home.id))
    assert deleted.status_code == 200, deleted.text
    with SessionLocal() as session:
        live = session.scalars(
            select(Episode).where(
                Episode.organization_id == organization.id, Episode.deleted_at.is_(None)
            )
        ).all()
        assert [episode.workspace_id for episode in live] == [home.id]
        memories = session.scalars(
            select(Memory).where(
                Memory.organization_id == organization.id, Memory.status == "active"
            )
        ).all()
        assert memories and {m.episode_id for m in memories} == {live[0].id}


async def test_memories_can_be_deleted_with_the_workspace() -> None:
    _org_key, organization, _home, sibling, _home_key, sibling_key = _tenant()
    memory = (await _store(sibling_key, "Sibling secret plan: the Narwhal launch date")).json()
    await _relate(sibling_key, "Narwhal Launch", "Narwhal Team")
    drain_jobs()
    plain_manager = _manager(organization)
    refused = await _delete(plain_manager, sibling, memories="delete")
    assert refused.status_code == 403
    with SessionLocal() as session:
        assert session.get(Workspace, sibling.id) is not None

    deleted = await _delete(_manager(organization, "memory.delete"), sibling, memories="delete")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["records"]["relationships"] == 1
    with SessionLocal() as session:
        row = session.get(Memory, UUID(memory["id"]))
        assert row is not None and row.status == "deleted" and row.content == ""
    admin = create_principal_key(organization, None, "Org reader", READ_WRITE)
    assert ("Narwhal Launch", "Narwhal Team") not in await _edges(admin, "narwhal")


async def test_delete_validates_its_choice_and_its_caller() -> None:
    _org_key, organization, home, sibling, _home_key, _sibling_key = _tenant()
    manager = _manager(organization)
    assert (await _delete(manager, sibling, memories="workspace")).status_code == 422
    assert (
        await _delete(manager, sibling, memories="workspace", to=str(sibling.id))
    ).status_code == 422
    assert (
        await _delete(manager, sibling, memories="organization", to=str(home.id))
    ).status_code == 422
    assert (await _delete(manager, sibling, memories="archive")).status_code == 422
    assert (await _delete(manager, sibling)).status_code == 422
    home_manager = create_principal_key(organization, home, "Home manager", ["keys.manage"])
    assert (await _delete(home_manager, sibling, memories="organization")).status_code == 403
    _, other, other_home, *_ = _tenant()
    assert (
        await _delete(manager, sibling, memories="workspace", to=str(other_home.id))
    ).status_code == 404
    assert (await _delete(_manager(other), sibling, memories="organization")).status_code == 404

    async with _client(manager) as client:
        listing = (await client.get("/v1/settings/workspaces")).json()
    counts = {row["name"]: row["credentials"] for row in listing["items"]}
    assert counts["Sibling"] == 1

"""Organization-wide memories are shared by every workspace in the organization.

A workspace-scoped key reads its own workspace plus the shared records, writes
into its workspace by default, and needs memory.write.organization to write or
change a shared record. Credentials are never issued wider than the issuer.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
import pytest

from albert.api import app
from albert.db import SessionLocal
from albert.models import Workspace

from .conftest import create_identity, create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio

READ_WRITE = [
    "memory.read",
    "memory.write",
    "memory.delete",
    "memory.export",
    "graph.query",
    "graph.write",
]


def _client(key: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers
    )


def _tenant():  # type: ignore[no-untyped-def]
    """An organization with two workspaces, an organization-wide key and one key per workspace."""
    org_key, organization, home = create_identity("Shared tenant", workspace_scoped=False)
    with SessionLocal() as session:
        sibling = Workspace(organization_id=organization.id, name="Sibling")
        session.add(sibling)
        session.commit()
        session.refresh(sibling)
    home_key = create_principal_key(organization, home, "Home agent", READ_WRITE)
    sibling_key = create_principal_key(organization, sibling, "Sibling agent", READ_WRITE)
    return org_key, organization, home, sibling, home_key, sibling_key


async def _store(key: str, content: str, **extra: object) -> httpx.Response:
    async with _client(key) as client:
        return await client.post("/v1/memories", json={"content": content, **extra})


async def test_workspace_key_reads_its_workspace_and_the_shared_memories() -> None:
    org_key, _org, _home, _sibling, home_key, sibling_key = _tenant()
    shared = (await _store(org_key, "Shared glossary: Albatross means the billing service")).json()
    home = (await _store(home_key, "Home plan: Albatross migration first")).json()
    theirs = (await _store(sibling_key, "Sibling plan: Albatross budget cut")).json()
    assert home["workspace_id"] is not None and shared["workspace_id"] is None
    drain_jobs()

    async with _client(home_key) as client:
        assert (await client.get(f"/v1/memories/{shared['id']}")).status_code == 200
        assert (await client.get(f"/v1/memories/{theirs['id']}")).status_code == 404
        found = await client.post("/v1/search", json={"query": "Albatross", "limit": 20})
        ids = {hit["memory_id"] for hit in found.json()["hits"]}
        assert {shared["id"], home["id"]} <= ids
        assert theirs["id"] not in ids
        context = await client.post("/v1/context/assemble", json={"query": "Albatross"})
        assert "Shared glossary" in context.json()["context"]
        assert "Sibling plan" not in context.json()["context"]
        exported = (await client.get("/v1/export")).json()
        assert {m["ref"] for m in exported["memories"]} == {shared["id"], home["id"]}


async def test_sharing_from_a_workspace_needs_memory_write_organization() -> None:
    _org_key, organization, home, _sibling, home_key, sibling_key = _tenant()
    refused = await _store(home_key, "Everyone should know this", scope="organization")
    assert refused.status_code == 403
    assert "memory.write.organization" in refused.text

    publisher = create_principal_key(
        organization, home, "Publisher", [*READ_WRITE, "memory.write.organization"]
    )
    published = await _store(publisher, "Release freeze starts Friday", scope="organization")
    assert published.status_code == 201, published.text
    assert published.json()["workspace_id"] is None
    async with _client(sibling_key) as client:
        assert (await client.get(f"/v1/memories/{published.json()['id']}")).status_code == 200

    both = await _store(publisher, "x", scope="organization", workspace_id=str(home.id))
    assert both.status_code == 422


async def test_shared_memories_change_only_with_the_grant() -> None:
    org_key, organization, home, _sibling, home_key, _sibling_key = _tenant()
    shared = (await _store(org_key, "Office closes at six")).json()["id"]
    async with _client(home_key) as client:
        edit = await client.patch(f"/v1/memories/{shared}", json={"content": "Office never closes"})
        assert edit.status_code == 403
        assert (await client.delete(f"/v1/memories/{shared}")).status_code == 403
        assert (await client.get(f"/v1/memories/{shared}")).json()["content"] == (
            "Office closes at six"
        )

    editor = create_principal_key(
        organization, home, "Editor", [*READ_WRITE, "memory.write.organization"]
    )
    async with _client(editor) as client:
        edit = await client.patch(f"/v1/memories/{shared}", json={"content": "Office closes at 7"})
        assert edit.status_code == 200
        assert (await client.delete(f"/v1/memories/{shared}")).status_code == 204


async def test_shared_episodes_are_readable_and_guarded_like_memories() -> None:
    org_key, _organization, _home, _sibling, home_key, _sibling_key = _tenant()
    async with _client(org_key) as client:
        posted = await client.post(
            "/v1/episodes", json={"content": "All-hands notes", "source_uri": "test://shared"}
        )
        episode = posted.json()["id"]
    async with _client(home_key) as client:
        assert (await client.get(f"/v1/episodes/{episode}")).status_code == 200
        assert (await client.delete(f"/v1/episodes/{episode}")).status_code == 403


async def test_graph_reads_include_shared_edges_but_not_a_sibling_workspace() -> None:
    org_key, _organization, _home, _sibling, home_key, sibling_key = _tenant()

    async def relate(key: str, source: str, target: str) -> None:
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

    await relate(org_key, "Zebra Gateway", "Zebra Auth")
    await relate(home_key, "Zebra Portal", "Zebra Gateway")
    await relate(sibling_key, "Zebra Ledger", "Zebra Vault")
    async with _client(home_key) as client:
        edges = (await client.post("/v1/graph/query", json={"query": "zebra"})).json()
    names = {(edge["source_name"], edge["target_name"]) for edge in edges}
    assert ("Zebra Gateway", "Zebra Auth") in names
    assert ("Zebra Portal", "Zebra Gateway") in names
    assert ("Zebra Ledger", "Zebra Vault") not in names


async def test_credentials_are_issued_no_wider_than_the_issuer() -> None:
    _org_key, organization, home, sibling, _home_key, _sibling_key = _tenant()
    org_manager = create_principal_key(organization, None, "Org manager", ["keys.manage"])
    home_manager = create_principal_key(organization, home, "Home manager", ["keys.manage"])

    async with _client(org_manager) as client:
        minted = await client.post(
            "/v1/settings/api-keys",
            json={
                "name": "sibling reader",
                "capabilities": ["memory.read"],
                "workspace_id": str(sibling.id),
            },
        )
        assert minted.status_code == 201, minted.text
        assert minted.json()["workspace_name"] == "Sibling"
        listing = (await client.get("/v1/settings/credentials")).json()
        assert listing["workspace_id"] is None
        assert "memory.write.organization" in listing["grantable_capabilities"]
        rows = {row["name"]: row for row in listing["api_keys"]}
        assert rows["sibling reader"]["workspace_id"] == str(sibling.id)

    async with _client(home_manager) as client:
        listing = (await client.get("/v1/settings/credentials")).json()
        assert listing["workspace_id"] == str(home.id)
        assert "memory.write.organization" not in listing["grantable_capabilities"]
        elsewhere = await client.post(
            "/v1/settings/api-keys",
            json={"name": "x", "capabilities": ["memory.read"], "workspace_id": str(sibling.id)},
        )
        assert elsewhere.status_code == 403
        sharing = await client.post(
            "/v1/settings/api-keys",
            json={"name": "x", "capabilities": ["memory.write", "memory.write.organization"]},
        )
        assert sharing.status_code == 403
        own = await client.post(
            "/v1/settings/api-keys", json={"name": "home reader", "capabilities": ["memory.read"]}
        )
        assert own.json()["workspace_id"] == str(home.id)
        # It sees and revokes only its own workspace's credentials.
        listing = (await client.get("/v1/settings/credentials")).json()
        assert {row["workspace_id"] for row in listing["api_keys"]} == {str(home.id)}
        sibling_key = rows["sibling reader"]["id"]
        assert (await client.delete(f"/v1/settings/api-keys/{sibling_key}")).status_code == 404


async def test_a_link_issued_into_a_workspace_redeems_into_it() -> None:
    _org_key, organization, _home, sibling, _home_key, _sibling_key = _tenant()
    manager = create_principal_key(organization, None, "Org manager", ["keys.manage"])
    async with _client(manager) as client:
        created = await client.post(
            "/v1/settings/connection-links",
            json={
                "name": "sibling agent",
                "capabilities": ["memory.read", "memory.write"],
                "workspace_id": str(sibling.id),
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["workspace_name"] == "Sibling"
    async with _client() as anonymous:
        redeemed = await anonymous.post(
            f"{urlparse(created.json()['url']).path}/redeem", json={"agent_name": "agent"}
        )
    credential = redeemed.json()["credential"]
    stored = (await _store(credential, "Lands in the sibling workspace")).json()
    assert stored["workspace_id"] == str(sibling.id)


async def test_workspaces_are_managed_by_organization_wide_keys_only() -> None:
    org_key, organization, home, _sibling, _home_key, _sibling_key = _tenant()
    await _store(org_key, "One shared memory")
    org_manager = create_principal_key(organization, None, "Org manager", ["keys.manage"])
    home_manager = create_principal_key(organization, home, "Home manager", ["keys.manage"])

    async with _client(org_manager) as client:
        created = await client.post("/v1/settings/workspaces", json={"name": "  Research  "})
        assert created.status_code == 201, created.text
        assert created.json()["name"] == "Research"
        duplicate = await client.post("/v1/settings/workspaces", json={"name": "Research"})
        assert duplicate.status_code == 409
        renamed = await client.patch(
            f"/v1/settings/workspaces/{created.json()['id']}", json={"name": "Lab"}
        )
        assert renamed.json()["name"] == "Lab"
        listing = (await client.get("/v1/settings/workspaces")).json()
        assert {row["name"] for row in listing["items"]} >= {"Default", "Sibling", "Lab"}
        assert listing["shared_memories"] >= 1
        assert listing["can_manage"] is True

    async with _client(home_manager) as client:
        listing = (await client.get("/v1/settings/workspaces")).json()
        assert [row["id"] for row in listing["items"]] == [str(home.id)]
        assert listing["can_manage"] is False
        assert (
            await client.post("/v1/settings/workspaces", json={"name": "Nope"})
        ).status_code == 403


async def test_console_scope_and_shared_only_map() -> None:
    org_key, organization, home, _sibling, home_key, _sibling_key = _tenant()
    shared = (await _store(org_key, "Shared runbook")).json()["id"]
    own = (await _store(home_key, "Home runbook")).json()["id"]
    console_key = create_principal_key(organization, home, "Console", ["console.read"])
    org = {"organization_id": str(organization.id)}
    async with _client(console_key) as console:
        scope = (await console.get("/v1/console/scope")).json()
        assert scope["admin"] is False
        assert scope["organization"]["id"] == str(organization.id)
        assert scope["workspace"] == {"id": str(home.id), "name": home.name}
        both = (await console.get("/v1/console/map", params=org)).json()["nodes"]
        assert {node["id"] for node in both} == {shared, own}
        only = (await console.get("/v1/console/map", params={**org, "shared_only": "true"})).json()
        assert {node["id"] for node in only["nodes"]} == {shared}

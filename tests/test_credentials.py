from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx
import pytest

from albert.api import app
from albert.db import SessionLocal
from albert.models import AuditEvent, ConnectionLink
from albert.trace_writer import get_trace_writer

from .conftest import create_principal_key

pytestmark = pytest.mark.anyio


def _client(key: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers
    )


@pytest.fixture()
def manager_key(identity):  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    return create_principal_key(organization, None, "Console", ["console.read", "keys.manage"])


async def _link(manager: httpx.AsyncClient, capabilities: list[str], **extra) -> str:
    response = await manager.post(
        "/v1/settings/connection-links",
        json={"name": "Albert Cloud", "capabilities": capabilities, **extra},
    )
    assert response.status_code == 201, response.text
    return urlparse(response.json()["url"]).path


async def test_settings_require_keys_manage(identity) -> None:  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    reader = create_principal_key(organization, None, "Reader", ["console.read", "memory.read"])
    async with _client(reader) as client:
        assert (await client.get("/v1/settings/credentials")).status_code == 403
        response = await client.post(
            "/v1/settings/api-keys", json={"name": "x", "capabilities": ["memory.read"]}
        )
        assert response.status_code == 403


async def test_create_list_and_revoke_api_key(manager_key: str) -> None:
    async with _client(manager_key) as manager:
        created = await manager.post(
            "/v1/settings/api-keys",
            json={"name": "Research agent", "capabilities": ["memory.read", "graph.query"]},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["credential"].startswith("alb_")
        assert body["capabilities"] == ["graph.query", "memory.read"]

        async with _client(body["credential"]) as agent:
            assert (await agent.post("/v1/search", json={"query": "probe"})).status_code == 200
            assert (await agent.get("/v1/settings/credentials")).status_code == 403

        listing = (await manager.get("/v1/settings/credentials")).json()
        row = next(item for item in listing["api_keys"] if item["id"] == body["id"])
        assert "credential" not in row and row["active"] is True
        assert "admin" not in listing["grantable_capabilities"]
        assert "keys.manage" not in listing["grantable_capabilities"]

        assert (await manager.delete(f"/v1/settings/api-keys/{body['id']}")).status_code == 204
        async with _client(body["credential"]) as agent:
            assert (await agent.post("/v1/search", json={"query": "probe"})).status_code == 401


@pytest.mark.parametrize("capability", ["admin", "keys.manage", "made.up"])
async def test_cannot_grant_escalating_or_unknown_capabilities(
    manager_key: str, capability: str
) -> None:
    async with _client(manager_key) as manager:
        for path in ("/v1/settings/api-keys", "/v1/settings/connection-links"):
            response = await manager.post(path, json={"name": "x", "capabilities": [capability]})
            assert response.status_code == 422


async def test_console_cannot_revoke_its_own_key(manager_key: str) -> None:
    async with _client(manager_key) as manager:
        listing = (await manager.get("/v1/settings/credentials")).json()
        own = next(item for item in listing["api_keys"] if item["current"])
        assert (await manager.delete(f"/v1/settings/api-keys/{own['id']}")).status_code == 409


async def test_link_redeems_once_with_albert_cloud_contract(manager_key: str) -> None:
    async with _client(manager_key) as manager:
        path = await _link(manager, ["console.read"])
    async with _client() as anonymous:
        manifest = (await anonymous.get(f"{path}/manifest")).json()
        assert manifest["version"] == "1"
        assert manifest["capabilities"] == ["console.read"]
        redeem_path = urlparse(manifest["redeem_url"]).path
        assert redeem_path == f"{path}/redeem"

        redeemed = await anonymous.post(
            redeem_path,
            json={"agent_name": "Albert Cloud console", "capabilities": ["console.read"]},
        )
        assert redeemed.status_code == 201
        body = redeemed.json()
        assert {"credential", "rest_url", "mcp_url"} <= body.keys()

        again = await anonymous.post(redeem_path, json={"agent_name": "replay"})
        assert again.status_code == 410
        assert (await anonymous.get(f"{path}/manifest")).status_code == 410

    async with _client(body["credential"]) as cloud:
        assert (await cloud.get("/v1/console/overview")).status_code == 200
        assert (await cloud.get("/v1/settings/credentials")).status_code == 403


async def test_redeem_cannot_exceed_link(manager_key: str) -> None:
    async with _client(manager_key) as manager:
        path = await _link(manager, ["memory.read"])
    async with _client() as anonymous:
        denied = await anonymous.post(
            f"{path}/redeem", json={"agent_name": "greedy", "capabilities": ["memory.write"]}
        )
        assert denied.status_code == 403
        # A refused redeem does not burn the link.
        ok = await anonymous.post(f"{path}/redeem", json={"agent_name": "fine"})
        assert ok.status_code == 201
        assert ok.json()["capabilities"] == ["memory.read"]


async def test_revoked_and_expired_links_are_gone(manager_key: str) -> None:
    async with _client(manager_key) as manager:
        revoked = await _link(manager, ["memory.read"])
        listing = (await manager.get("/v1/settings/credentials")).json()
        link_id = listing["connection_links"][0]["id"]
        assert (await manager.delete(f"/v1/settings/connection-links/{link_id}")).status_code == 204
        expired = await _link(manager, ["memory.read"])
    with SessionLocal() as session:
        link = session.query(ConnectionLink).order_by(ConnectionLink.created_at.desc()).first()
        link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    async with _client() as anonymous:
        for path in (revoked, expired):
            assert (await anonymous.get(path)).status_code == 410
            response = await anonymous.post(f"{path}/redeem", json={"agent_name": "late"})
            assert response.status_code == 410
    async with _client(manager_key) as manager:
        states = {
            item["state"]
            for item in (await manager.get("/v1/settings/credentials")).json()["connection_links"]
        }
        assert {"revoked", "expired"} <= states


async def test_concurrent_redeems_mint_one_key(manager_key: str) -> None:
    async with _client(manager_key) as manager:
        path = await _link(manager, ["memory.read"])
    async with _client() as anonymous:
        responses = await asyncio.gather(
            *(anonymous.post(f"{path}/redeem", json={"agent_name": f"a{i}"}) for i in range(5))
        )
    assert sorted(response.status_code for response in responses).count(201) == 1


async def test_link_tokens_never_reach_traces_and_actions_are_audited(manager_key: str) -> None:
    async with _client(manager_key) as manager:
        path = await _link(manager, ["memory.read"])
    token = path.rsplit("/", 1)[1]
    async with _client() as anonymous:
        await anonymous.post(f"{path}/redeem", json={"agent_name": "traced"})
    get_trace_writer().flush()
    async with _client(manager_key) as manager:
        traces = (await manager.get("/v1/console/traces", params={"limit": 200})).text
    assert token not in traces
    with SessionLocal() as session:
        events = {row.event_type for row in session.query(AuditEvent).all()}
    assert {"connection_link.create", "connection_link.redeem"} <= events

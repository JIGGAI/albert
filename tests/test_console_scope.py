"""A console.read key reads only its own tenant, and only its own workspace when scoped.

keys.manage holders and connection links can issue console.read, so a console
key must never reach further than the credential that issued it. Only admin
crosses tenants.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import ClassVar

import httpx
import pytest

from albert import services
from albert.api import app
from albert.db import SessionLocal
from albert.models import Trace, Workspace

from .conftest import create_identity, create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio


def _client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    )


async def _remember(key: str, content: str, **extra: str) -> str:
    async with _client(key) as client:
        response = await client.post(
            "/v1/memories", json={"content": content, "sensitivity": "restricted", **extra}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]


async def _minted_console_key(manager: str) -> str:
    async with _client(manager) as client:
        minted = await client.post(
            "/v1/settings/api-keys", json={"name": "console", "capabilities": ["console.read"]}
        )
        assert minted.status_code == 201, minted.text
        return minted.json()["credential"]


def _trace(organization_id) -> str:  # type: ignore[no-untyped-def]
    with SessionLocal() as session:
        trace = Trace(
            kind="request",
            name="POST /v1/search",
            organization_id=organization_id,
            status="ok",
            started_at=datetime.now(UTC),
            duration_ms=1,
            summary={"query": "scope probe"},
            spans=[],
        )
        session.add(trace)
        session.commit()
        return str(trace.id)


async def test_minted_console_key_cannot_read_another_tenant() -> None:
    _key_a, org_a, ws_a = create_identity("Tenant A")
    manager = create_principal_key(org_a, ws_a, "A manager", ["keys.manage"])
    key_b, org_b, _ws_b = create_identity("Tenant B")
    secret = await _remember(key_b, "Tenant B acquisition target is Zeta Corp")
    trace_b = _trace(org_b.id)
    console_key = await _minted_console_key(manager)
    other = {"organization_id": str(org_b.id)}

    async with _client(console_key) as console:
        assert (await console.get(f"/v1/console/memories/{secret}")).status_code == 404
        assert (await console.get(f"/v1/console/traces/{trace_b}")).status_code == 404
        for path in ("graph", "map", "workspaces", "memory"):
            response = await console.get(f"/v1/console/{path}", params=other)
            assert response.status_code == 404, path
        for path in ("traces", "agents", "retrieval"):
            response = await console.get(f"/v1/console/{path}", params=other)
            assert response.status_code == 404, path
        search = await console.post(
            "/v1/console/search", json={**other, "query": "acquisition target"}
        )
        assert search.status_code == 404

        organizations = (await console.get("/v1/console/organizations")).json()["items"]
        assert [row["id"] for row in organizations] == [str(org_a.id)]
        traces = (await console.get("/v1/console/traces", params={"limit": 200})).json()
        assert {item["organization_id"] for item in traces["items"]} <= {str(org_a.id)}


async def test_workspace_scoped_console_key_stays_in_its_workspace() -> None:
    key, organization, workspace = create_identity("Two workspaces", workspace_scoped=False)
    with SessionLocal() as session:
        sibling = Workspace(organization_id=organization.id, name="Sibling")
        session.add(sibling)
        session.commit()
        sibling_id = str(sibling.id)
    own = await _remember(key, "Own workspace plan", workspace_id=str(workspace.id))
    hidden = await _remember(key, "Sibling workspace payroll", workspace_id=sibling_id)
    unscoped = await _remember(key, "Organization-wide memory")
    # Albert Cloud's console key has this shape: console.read on a workspace principal.
    console_key = create_principal_key(organization, workspace, "Control Plane", ["console.read"])
    org = {"organization_id": str(organization.id)}

    async with _client(console_key) as console:
        assert (await console.get(f"/v1/console/memories/{own}")).status_code == 200
        assert (await console.get(f"/v1/console/memories/{hidden}")).status_code == 404
        assert (await console.get(f"/v1/console/memories/{unscoped}")).status_code == 404
        sibling_map = await console.get(
            "/v1/console/map", params={**org, "workspace_id": sibling_id}
        )
        assert sibling_map.status_code == 404
        nodes = (await console.get("/v1/console/map", params=org)).json()["nodes"]
        assert {node["id"] for node in nodes} == {own}
        workspaces = (await console.get("/v1/console/workspaces", params=org)).json()
        assert [row["id"] for row in workspaces["items"]] == [str(workspace.id)]
        assert workspaces["unscoped_memories"] == 0


async def test_admin_console_key_still_crosses_tenants() -> None:
    _key_a, org_a, _ws_a = create_identity("Admin home")
    key_b, _org_b, _ws_b = create_identity("Admin other")
    secret = await _remember(key_b, "Visible to the instance administrator")
    admin = create_principal_key(org_a, None, "Operator", ["admin"])

    async with _client(admin) as console:
        detail = await console.get(f"/v1/console/memories/{secret}")
        assert detail.status_code == 200
        assert detail.json()["content"] == "Visible to the instance administrator"
        organizations = (await console.get("/v1/console/organizations")).json()["items"]
        assert len(organizations) > 1


async def test_stream_only_emits_the_keys_own_tenant() -> None:
    from albert.console import stream
    from albert.security import AuthContext

    _key_a, org_a, _ws_a = create_identity("Stream other")
    _key_b, org_b, _ws_b = create_identity("Stream own")
    _trace(org_a.id)
    own_trace = _trace(org_b.id)

    class FakeRequest:
        headers: ClassVar[dict[str, str]] = {
            "last-event-id": "1970-01-01T00:00:00+00:00|00000000-0000-0000-0000-000000000000"
        }

    class FakeSession:
        def close(self) -> None:
            return None

    auth = AuthContext(
        principal_id=org_b.id,
        organization_id=org_b.id,
        workspace_id=None,
        capabilities=frozenset({"console.read"}),
    )
    body = stream(FakeRequest(), auth, FakeSession()).body_iterator  # type: ignore[arg-type]
    seen: set[str | None] = set()
    async for chunk in body:
        if str(chunk).startswith(":"):
            continue
        payload = json.loads(str(chunk).split("data: ", 1)[1].strip())
        seen.add(payload["organization_id"])
        if payload["id"] == own_trace:
            break
    await body.aclose()
    assert seen == {str(org_b.id)}


async def test_episode_inherits_a_raised_classification(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    real = services.classify
    monkeypatch.setattr(
        services,
        "classify",
        lambda text: dataclasses.replace(real(text), sensitivity="restricted"),
    )
    key, _organization, _workspace = create_identity("Internal-only reader")
    async with _client(key) as client:
        posted = await client.post(
            "/v1/episodes",
            json={"content": "Board approved the layoff list for Q3", "source_uri": "test://raise"},
        )
        assert posted.status_code == 201, posted.text
        drain_jobs()
        # The key cannot read restricted material, so neither copy may be served.
        assert (await client.get(f"/v1/episodes/{posted.json()['id']}")).status_code == 404

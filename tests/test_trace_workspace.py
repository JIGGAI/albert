"""Every trace records the workspace it ran in, and console reads honour it.

A request takes the key's workspace, a job the workspace of the record it
processed, and organization-wide work has none. A workspace-scoped console key
sees only its own workspace's traces; an organization-wide one can filter by
workspace.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from albert.api import app
from albert.db import SessionLocal
from albert.models import Trace, Workspace
from albert.trace_writer import get_trace_writer

from .conftest import create_identity, create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio


def _client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    )


def _tenant():  # type: ignore[no-untyped-def]
    org_key, organization, home = create_identity("Traced tenant", workspace_scoped=False)
    with SessionLocal() as session:
        sibling = Workspace(organization_id=organization.id, name="Sibling")
        session.add(sibling)
        session.commit()
        session.refresh(sibling)
    caps = ["memory.read", "memory.write"]
    home_key = create_principal_key(organization, home, "Home agent", caps)
    sibling_key = create_principal_key(organization, sibling, "Sibling agent", caps)
    return org_key, organization, home, sibling, home_key, sibling_key


async def _search(key: str, query: str) -> None:
    async with _client(key) as client:
        assert (await client.post("/v1/search", json={"query": query})).status_code == 200


def _traces(organization_id: UUID) -> list[Trace]:
    get_trace_writer().flush()
    with SessionLocal() as session:
        return list(session.scalars(select(Trace).where(Trace.organization_id == organization_id)))


async def test_requests_and_jobs_record_their_workspace() -> None:
    org_key, organization, home, _sibling, home_key, _sibling_key = _tenant()
    await _search(home_key, "home probe")
    await _search(org_key, "organization probe")
    async with _client(home_key) as client:
        memory = (await client.post("/v1/memories", json={"content": "Home fact"})).json()
    drain_jobs()

    traces = _traces(organization.id)
    by_query = {t.summary.get("query"): t for t in traces if t.summary.get("query")}
    assert by_query["home probe"].workspace_id == home.id
    assert by_query["organization probe"].workspace_id is None
    jobs = [
        t
        for t in traces
        if t.kind == "job" and t.summary.get("payload", {}).get("memory_id") == memory["id"]
    ]
    assert jobs and all(job.workspace_id == home.id for job in jobs)


async def test_workspace_console_key_sees_only_its_workspace_traces() -> None:
    org_key, organization, home, sibling, home_key, sibling_key = _tenant()
    await _search(home_key, "home only")
    await _search(sibling_key, "sibling only")
    await _search(org_key, "organization only")
    traces = {t.summary.get("query"): t for t in _traces(organization.id)}

    home_console = create_principal_key(organization, home, "Home console", ["console.read"])
    async with _client(home_console) as console:
        listed = (await console.get("/v1/console/traces", params={"limit": 200})).json()["items"]
        queries = {item["summary"].get("query") for item in listed}
        assert "home only" in queries
        assert not {"sibling only", "organization only"} & queries
        assert {item["workspace_id"] for item in listed} == {str(home.id)}
        sibling_trace = traces["sibling only"].id
        assert (await console.get(f"/v1/console/traces/{sibling_trace}")).status_code == 404
        named = await console.get("/v1/console/traces", params={"workspace_id": str(sibling.id)})
        assert named.status_code == 404
        agents = (await console.get("/v1/console/ops/agents", params={"hours": 1})).json()
        assert {row["name"] for row in agents["principals"]} <= {"Home agent", "Home console"}

    org_console = create_principal_key(organization, None, "Org console", ["console.read"])
    async with _client(org_console) as console:
        everything = (await console.get("/v1/console/traces", params={"limit": 200})).json()
        assert {"home only", "sibling only", "organization only"} <= {
            item["summary"].get("query") for item in everything["items"]
        }
        narrowed = await console.get(
            "/v1/console/traces", params={"workspace_id": str(sibling.id), "limit": 200}
        )
        assert {item["summary"].get("query") for item in narrowed.json()["items"]} == {
            "sibling only"
        }


async def test_stream_drops_other_workspaces() -> None:
    from albert.console import stream
    from albert.security import AuthContext

    _org_key, organization, home, sibling, _home_key, _sibling_key = _tenant()
    with SessionLocal() as session:
        for workspace in (sibling.id, None, home.id):
            session.add(
                Trace(
                    kind="request",
                    name="POST /v1/search",
                    organization_id=organization.id,
                    workspace_id=workspace,
                    status="ok",
                    started_at=datetime.now(UTC),
                    duration_ms=1,
                    summary={},
                    spans=[],
                )
            )
            session.commit()
        own = str(
            session.scalar(
                select(Trace.id).where(
                    Trace.organization_id == organization.id, Trace.workspace_id == home.id
                )
            )
        )

    class FakeRequest:
        headers: ClassVar[dict[str, str]] = {
            "last-event-id": "1970-01-01T00:00:00+00:00|00000000-0000-0000-0000-000000000000"
        }

    class FakeSession:
        def close(self) -> None:
            return None

    auth = AuthContext(
        principal_id=organization.id,
        organization_id=organization.id,
        workspace_id=home.id,
        capabilities=frozenset({"console.read"}),
    )
    body = stream(FakeRequest(), auth, FakeSession()).body_iterator  # type: ignore[arg-type]
    seen: set[str | None] = set()
    async for chunk in body:
        if str(chunk).startswith(":"):
            continue
        payload = json.loads(str(chunk).split("data: ", 1)[1].strip())
        seen.add(payload["workspace_id"])
        if payload["id"] == own:
            break
    await body.aclose()
    assert seen == {str(home.id)}

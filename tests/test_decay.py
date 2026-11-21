from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from albert.api import app
from albert.db import SessionLocal
from albert.models import AuditEvent, DecayPolicy, Memory

from .conftest import create_identity, create_principal_key

pytestmark = pytest.mark.anyio


async def _admin_client(name: str = "Decay Admin") -> tuple[httpx.AsyncClient, object, object]:
    _key, organization, workspace = create_identity(name)
    admin_key = create_principal_key(organization, workspace, "Admin", ["admin"])
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    return client, organization, workspace


async def _memory(
    client: httpx.AsyncClient,
    content: str,
    *,
    valid_from: datetime,
    memory_type: str = "preference",
    metadata: dict | None = None,
) -> dict:
    response = await client.post(
        "/v1/memories",
        json={
            "content": content,
            "memory_type": memory_type,
            "valid_from": valid_from.isoformat(),
            "metadata": metadata or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _rules(half_life: float = 10) -> dict:
    return {
        "default": {
            "freshness_days": 10,
            "half_life_days": half_life,
            "retention_days": 60,
            "stale_state": "demote",
        },
        "durable": {
            "freshness_days": 0,
            "half_life_days": 1,
            "retention_days": 1,
            "protected": True,
        },
    }


async def test_decay_is_deterministic_explainable_and_never_deletes_evidence() -> None:
    client, organization, _workspace = await _admin_client()
    async with client:
        as_of = datetime(2026, 11, 20, tzinfo=UTC)
        stale = await _memory(
            client, "Prefers deterministic release notes", valid_from=as_of - timedelta(days=30)
        )
        fresh = await _memory(
            client, "Deterministic release notes are concise", valid_from=as_of
        )
        pinned = await _memory(
            client,
            "Pinned release ownership record",
            valid_from=as_of - timedelta(days=100),
            metadata={"pinned": True},
        )
        durable = await _memory(
            client,
            "Legal retention classification",
            valid_from=as_of - timedelta(days=100),
            memory_type="durable",
        )

        policy = await client.put(
            "/v1/decay/policy", json={"rules": _rules(), "expected_version": 0}
        )
        assert policy.status_code == 200, policy.text
        run = await client.post("/v1/decay/recompute", json={"as_of": as_of.isoformat()})
        assert run.status_code == 200, run.text
        retry = await client.post("/v1/decay/recompute", json={"as_of": as_of.isoformat()})
        assert retry.json() == run.json()
        states = {row["memory_id"]: row for row in run.json()["states"]}
        assert states[stale["id"]]["score_multiplier"] == pytest.approx(0.25)
        assert states[stale["id"]]["reason"] == "freshness_elapsed"
        assert states[pinned["id"]]["protected"] is True
        assert states[pinned["id"]]["reason"] == "pinned"
        assert states[durable["id"]]["reason"] == "protected_class"

        search = await client.post(
            "/v1/search",
            json={
                "query": "deterministic release notes",
                "include_graph": False,
                "include_content": False,
            },
        )
        hit = next(row for row in search.json()["hits"] if row["memory_id"] == stale["id"])
        positions = {row["memory_id"]: index for index, row in enumerate(search.json()["hits"])}
        assert positions[fresh["id"]] < positions[stale["id"]]
        assert hit["metadata"]["decay"] == {
            "policy_version": 1,
            "multiplier": pytest.approx(0.25),
            "stale": True,
            "protected": False,
            "reason": "freshness_elapsed",
            "evaluated_at": as_of.isoformat(),
        }

        profile = await client.post("/v1/profile/rebuild")
        assertion = next(
            row for row in profile.json()["assertions"] if stale["id"] in row["citations"]
        )
        assert assertion["decay_multiplier"] == pytest.approx(0.25)
        assert assertion["decay_reason"] == "freshness_elapsed"

    with SessionLocal() as session:
        assert (
            session.get(Memory, UUID(stale["id"])).content == "Prefers deterministic release notes"
        )
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.organization_id == organization.id,
                    AuditEvent.event_type == "decay.recomputed",
                )
            )
        )
        assert len(events) == 1
        assert "deterministic" not in str(events[0].detail)


async def test_policy_rollback_rebuild_and_scope_isolation() -> None:
    client, organization, workspace = await _admin_client("Rollback Admin")
    as_of = datetime(2026, 11, 20, tzinfo=UTC)
    async with client:
        memory = await _memory(client, "Rollback candidate", valid_from=as_of - timedelta(days=30))
        first = (
            await client.put("/v1/decay/policy", json={"rules": _rules(10), "expected_version": 0})
        ).json()
        baseline = (
            await client.post("/v1/decay/recompute", json={"as_of": as_of.isoformat()})
        ).json()["states"][0]["score_multiplier"]
        second = await client.put(
            "/v1/decay/policy", json={"rules": _rules(20), "expected_version": 1}
        )
        assert second.json()["version"] == 2
        conflict = await client.put(
            "/v1/decay/policy", json={"rules": _rules(30), "expected_version": 1}
        )
        assert conflict.status_code == 409
        rolled = await client.post("/v1/decay/policy/rollback", json={"version": first["version"]})
        assert rolled.json()["version"] == 3
        assert rolled.json()["rollback_of_version"] == 1
        rebuilt = (
            await client.post("/v1/decay/recompute", json={"as_of": as_of.isoformat()})
        ).json()
        assert rebuilt["states"][0]["score_multiplier"] == baseline

    foreign_key, _foreign_org, _foreign_workspace = create_identity("Foreign")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {foreign_key}"},
    ) as foreign:
        assert (await foreign.get(f"/v1/memories/{memory['id']}/decay")).status_code == 404

    under_scoped_key = create_principal_key(organization, workspace, "Reader", ["memory.read"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {under_scoped_key}"},
    ) as reader:
        assert (await reader.get(f"/v1/memories/{memory['id']}/decay")).status_code == 200
        assert (await reader.post("/v1/decay/recompute", json={})).status_code == 403

    with SessionLocal() as session:
        policies = list(
            session.scalars(
                select(DecayPolicy)
                .where(DecayPolicy.organization_id == organization.id)
                .order_by(DecayPolicy.version)
            )
        )
        assert [row.active for row in policies] == [False, False, True]


async def test_corroboration_refreshes_decay_freshness(client: httpx.AsyncClient) -> None:
    old = datetime.now(UTC) - timedelta(days=100)
    evidence = await _memory(client, "Customer prefers concise updates", valid_from=old)
    evolved = await client.post(
        "/v1/fact-evolutions",
        json={
            "idempotency_key": "freshness-proof",
            "evidence_memory_id": evidence["id"],
            "relation": "adds",
            "effective_from": datetime.now(UTC).isoformat(),
        },
    )
    result = await client.get(f"/v1/memories/{evolved.json()['result_memory_id']}")
    assert result.json()["metadata_"]["freshness_at"]

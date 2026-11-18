from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from albert.api import app
from albert.db import SessionLocal
from albert.models import AuditEvent, Principal, ProfileAssertion, Trace, Workspace
from albert.trace_writer import get_trace_writer

from .conftest import create_identity, create_principal_key

pytestmark = pytest.mark.anyio


async def _remember(client: httpx.AsyncClient, content: str, **fields: object) -> dict:
    response = await client.post(
        "/v1/memories",
        json={"content": content, "memory_type": "identity", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_profile_rebuild_is_cited_deduplicated_ordered_and_bounded(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/v1/profile/status")).json()["state"] == "never"
    first = await _remember(
        client,
        "Uses Python",
        confidence=0.7,
        metadata={"source_trust": "verified"},
    )
    duplicate = await _remember(
        client,
        "  Uses   Python  ",
        confidence=0.9,
        metadata={"source_trust": "trusted"},
    )
    await _remember(client, "Prefers concise output", memory_type="preference")
    await _remember(client, "Generic facts are not profile assertions", memory_type="fact")

    rebuilt = await client.post("/v1/profile/rebuild")
    assert rebuilt.status_code == 200, rebuilt.text
    body = rebuilt.json()
    assert [item["bucket"] for item in body["assertions"]] == ["identity", "preferences"]
    identity = body["assertions"][0]
    assert identity["value"] == "Uses Python"
    assert identity["trust"] == "trusted"
    assert identity["confidence"] == max(first["confidence"], duplicate["confidence"])
    assert identity["citations"] == sorted([first["id"], duplicate["id"]])
    assert identity["valid_from"] and identity["sensitivity"] == "internal"

    bounded = (await client.get("/v1/profile", params={"budget": 1})).json()
    assert bounded["total"] == 2
    assert bounded["truncated"] is True
    assert bounded["assertions"] == body["assertions"][:1]
    status = (await client.get("/v1/profile/status")).json()
    assert status["state"] == "current" and status["assertion_count"] == 2


async def test_profile_rebuild_equivalence_correction_deletion_and_temporal_filter(
    client: httpx.AsyncClient,
) -> None:
    memory = await _remember(client, "Lives in Berlin")
    expired = await _remember(
        client,
        "Expired goal",
        memory_type="goal",
        valid_from=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        valid_until=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )
    assert expired["id"]
    first = (await client.post("/v1/profile/rebuild")).json()
    first_projection = first["assertions"]

    with SessionLocal() as session:
        session.execute(delete(ProfileAssertion))
        session.commit()
    rebuilt = (await client.post("/v1/profile/rebuild")).json()
    assert rebuilt["assertions"] == first_projection
    assert rebuilt["source_digest"] == first["source_digest"]

    changed = await client.patch(
        f"/v1/memories/{memory['id']}", json={"content": "Lives in Lisbon"}
    )
    assert changed.status_code == 200
    corrected = (await client.post("/v1/profile/rebuild")).json()
    assert [item["value"] for item in corrected["assertions"]] == ["Lives in Lisbon"]
    assert corrected["source_digest"] != first["source_digest"]

    assert (await client.delete(f"/v1/memories/{memory['id']}")).status_code == 204
    after_delete = (await client.post("/v1/profile/rebuild")).json()
    assert after_delete["assertions"] == []
    with SessionLocal() as session:
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "profile.rebuilt")
        ).all()
    assert audits and all("value" not in event.detail for event in audits)


async def test_profile_capability_tenant_workspace_and_sensitivity_boundaries(
    identity,
) -> None:  # type: ignore[no-untyped-def]
    key, organization, workspace = identity
    with SessionLocal() as session:
        owner = session.scalar(
            select(Principal).where(
                Principal.organization_id == organization.id,
                Principal.name == "Test Agent",
            )
        )
        assert owner is not None
        owner_id = owner.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as owner_client:
        baseline = (await owner_client.post("/v1/profile/rebuild")).json()["source_digest"]
        await _remember(
            owner_client,
            "Restricted profile marker",
            sensitivity="restricted",
        )
        assert (await owner_client.post("/v1/profile/rebuild")).status_code == 200
        hidden = (await owner_client.get("/v1/profile")).json()
        assert hidden["assertions"] == []
        assert hidden["source_digest"] == baseline
        hidden_status = (await owner_client.get("/v1/profile/status")).json()
        assert hidden_status["assertion_count"] == 0
        assert hidden_status["source_digest"] == baseline

    blind_key = create_principal_key(organization, workspace, "Blind", ["memory.read"])
    reader_key = create_principal_key(organization, workspace, "Reader", ["profile.read"])
    for candidate, expected in ((blind_key, 403), (reader_key, 404)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {candidate}"},
        ) as scoped:
            response = await scoped.get(f"/v1/profiles/{owner_id}")
            assert response.status_code == expected

    with SessionLocal() as session:
        sibling = Workspace(organization_id=organization.id, name=f"Sibling-{uuid4()}")
        session.add(sibling)
        session.commit()
        session.refresh(sibling)
    sibling_key = create_principal_key(
        organization,
        sibling,
        "Sibling profile operator",
        ["profile.read", "profile.read.organization"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {sibling_key}"},
    ) as sibling_client:
        assert (await sibling_client.get(f"/v1/profiles/{owner_id}")).status_code == 404

    foreign_key, _foreign_org, _foreign_workspace = create_identity(f"Foreign-{uuid4()}")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {foreign_key}"},
    ) as foreign:
        assert (await foreign.get(f"/v1/profiles/{owner_id}")).status_code == 404

    rebuild_only = create_principal_key(
        organization, workspace, "Rebuilder", ["profile.rebuild"]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {rebuild_only}"},
    ) as rebuilder:
        assert (await rebuilder.post("/v1/profile/rebuild")).status_code == 200
        assert (await rebuilder.get("/v1/profile")).status_code == 403


async def test_profile_traces_contain_decisions_not_restricted_bodies(
    client: httpx.AsyncClient,
) -> None:
    marker = "PROFILE-RESTRICTED-BODY-MARKER"
    await _remember(client, marker, memory_type="constraint")
    assert (await client.post("/v1/profile/rebuild")).status_code == 200
    get_trace_writer().flush()
    with SessionLocal() as session:
        traces = session.scalars(select(Trace).where(Trace.name.like("%profile%"))).all()
    serialized = json.dumps([[trace.summary, trace.spans] for trace in traces])
    assert marker not in serialized
    assert "profile.materialize" in serialized
    assert "assertion_count" in serialized

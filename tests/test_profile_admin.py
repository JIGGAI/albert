from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from albert.api import app
from albert.db import SessionLocal
from albert.models import AuditEvent, Principal

from .conftest import create_identity, create_principal_key

pytestmark = pytest.mark.anyio


def _client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    )


async def test_admin_policy_discovery_correction_suppression_and_history(identity) -> None:  # type: ignore[no-untyped-def]
    owner_key, organization, _workspace = identity
    async with _client(owner_key) as owner:
        memory = await owner.post(
            "/v1/memories",
            json={"content": "PROFILE-PRIVATE-ORIGINAL", "memory_type": "identity"},
        )
        assert memory.status_code == 201
        assert (await owner.post("/v1/profile/rebuild")).status_code == 200
    with SessionLocal() as session:
        principal_id = session.scalar(
            select(Principal.id).where(
                Principal.organization_id == organization.id,
                Principal.name == "Test Agent",
            )
        )
        assert principal_id is not None

    admin_key = create_principal_key(organization, None, "Profile Admin", ["profile.admin"])
    async with _client(admin_key) as admin:
        principals = (await admin.get("/v1/profile-admin/principals")).json()
        assert principal_id.hex in {
            item["principal_id"].replace("-", "") for item in principals["items"]
        }

        policy_payload = {
            "expected_version": 0,
            "idempotency_key": "policy-v1",
            "buckets": {"identity": ["identity"], "preferences": ["preference"]},
        }
        policy = await admin.put("/v1/profile-admin/policy", json=policy_payload)
        assert policy.status_code == 200 and policy.json()["version"] == 1
        assert (
            await admin.put("/v1/profile-admin/policy", json=policy_payload)
        ).json() == policy.json()
        stale = await admin.put(
            "/v1/profile-admin/policy",
            json={**policy_payload, "idempotency_key": "stale-policy"},
        )
        assert stale.status_code == 409

        assertions = (
            await admin.get(f"/v1/profile-admin/principals/{principal_id}/assertions")
        ).json()["items"]
        assertion = assertions[0]
        correction = {
            "expected_version": 1,
            "idempotency_key": "correction-1",
            "value": "Corrected city is Lisbon",
        }
        corrected = await admin.post(
            f"/v1/profile-admin/assertions/{assertion['id']}/correct", json=correction
        )
        assert corrected.status_code == 200
        assert corrected.json()["decision_state"] == "corrected"
        retry = await admin.post(
            f"/v1/profile-admin/assertions/{assertion['id']}/correct", json=correction
        )
        assert retry.json() == corrected.json()

    async with _client(owner_key) as owner:
        assert (await owner.post("/v1/profile/rebuild")).status_code == 200
        profile = (await owner.get("/v1/profile")).json()
        assert profile["assertions"][0]["value"] == "Corrected city is Lisbon"

    async with _client(admin_key) as admin:
        assertion = (
            await admin.get(f"/v1/profile-admin/principals/{principal_id}/assertions")
        ).json()["items"][0]
        suppressed = await admin.post(
            f"/v1/profile-admin/assertions/{assertion['id']}/suppress",
            json={"expected_version": 2, "idempotency_key": "suppression-1"},
        )
        assert suppressed.status_code == 200 and suppressed.json()["suppressed"] is True
        history = (await admin.get(f"/v1/profile-admin/principals/{principal_id}/history")).json()
        assert [item["action"] for item in history["items"]] == ["correct", "suppress"]
        for item, decision_state in zip(
            history["items"], ["corrected", "suppressed"], strict=True
        ):
            assert item["confidence"] == assertion["confidence"]
            assert item["trust"] == assertion["trust"]
            assert item["valid_from"] == assertion["valid_from"]
            assert item["valid_until"] == assertion["valid_until"]
            assert item["sensitivity"] == assertion["sensitivity"]
            assert item["decision_state"] == decision_state

    async with _client(owner_key) as owner:
        assert (await owner.get("/v1/profile")).json()["assertions"] == []
    with SessionLocal() as session:
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type.like("profile.%"))
        ).all()
    assert "PROFILE-PRIVATE-ORIGINAL" not in str([event.detail for event in audits])


async def test_admin_contracts_fail_closed_for_under_scoped_and_foreign_callers(identity) -> None:  # type: ignore[no-untyped-def]
    _owner_key, organization, workspace = identity
    with SessionLocal() as session:
        principal_id = session.scalar(
            select(Principal.id).where(Principal.organization_id == organization.id)
        )
        assert principal_id is not None
    blind_key = create_principal_key(organization, workspace, "Blind admin", ["profile.read"])
    async with _client(blind_key) as blind:
        assert (await blind.get("/v1/profile-admin/principals")).status_code == 403
    _foreign_key, foreign_org, _foreign_workspace = create_identity(f"Foreign-{uuid4()}")
    foreign_admin = create_principal_key(foreign_org, None, "Foreign admin", ["profile.admin"])
    async with _client(foreign_admin) as foreign:
        response = await foreign.get(f"/v1/profile-admin/principals/{principal_id}/assertions")
        assert response.status_code == 404

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from albert.db import SessionLocal
from albert.models import AuditEvent, CaptureEvent, Episode, Job

from .conftest import create_identity, create_principal_key

pytestmark = pytest.mark.anyio


def _profile(profile_key: str, version: int = 1, **overrides) -> dict:  # type: ignore[no-untyped-def]
    value = {
        "profile_key": profile_key,
        "version": version,
        "allowed_event_classes": ["user_goal"],
        "metadata_allowlists": {"user_goal": ["project"]},
        "event_bounds": {"user_goal": {"max_content_chars": 100, "max_metadata_chars": 100}},
        "sensitivity_floor": "internal",
        "source_trust": "verified",
        "retention_days": 14,
        "raw_body_policy": "deny",
        "deny_reasons": {"event_class_not_allowed": "brain_policy_denied"},
    }
    value.update(overrides)
    return value


async def _start(
    client: httpx.AsyncClient,
    profile_key: str,
    *,
    headers: dict[str, str] | None = None,
    **extra,
) -> dict:  # type: ignore[no-untyped-def]
    value = {
        "client_session_id": f"client-{uuid4().hex}",
        "idempotency_key": f"start-{uuid4().hex}",
        "profile_key": profile_key,
        **extra,
    }
    response = await client.post("/v1/capture/sessions", json=value, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _event(
    event_class: str = "user_goal", *, sequence: int = 1, content: str = "Remember this"
) -> dict:
    return {
        "sequence": sequence,
        "idempotency_key": f"event-{uuid4().hex}",
        "event_class": event_class,
        "content": content,
        "sensitivity": "public",
        "metadata": {"project": "albert", "private_note": "DROP_ME"},
    }


async def test_brain_policy_and_local_opt_down_minimize_before_queue(
    client: httpx.AsyncClient,
) -> None:
    profile_key = f"minimize-{uuid4().hex}"
    created = await client.post("/v1/capture/profiles", json=_profile(profile_key))
    assert created.status_code == 201

    broader = await client.post(
        "/v1/capture/sessions",
        json={
            "client_session_id": f"broader-{uuid4().hex}",
            "idempotency_key": f"broader-{uuid4().hex}",
            "profile_key": profile_key,
            "local_event_classes": ["user_goal", "assistant_outcome"],
        },
    )
    assert broader.status_code == 422

    started = await _start(
        client,
        profile_key,
        local_event_classes=["user_goal"],
        local_metadata_allowlists={"user_goal": ["project"]},
    )
    response = await client.post(f"/v1/capture/sessions/{started['id']}/events", json=_event())
    assert response.status_code == 201
    assert response.json()["profile_key"] == profile_key
    assert response.json()["profile_version"] == 1
    assert response.json()["sensitivity"] == "internal"
    assert response.json()["source_trust"] == "verified"
    assert response.json()["retention_days"] == 14

    with SessionLocal() as session:
        event = session.scalar(
            select(CaptureEvent).where(CaptureEvent.session_id == UUID(started["id"]))
        )
        assert event is not None
        assert event.metadata_ == {"project": "albert"}
        assert event.sensitivity == "internal"
        episode = session.get(Episode, event.episode_id)
        assert episode is not None
        assert episode.sensitivity == "internal"
        assert episode.extra["project"] == "albert"
        assert "private_note" not in episode.extra
        assert "DROP_ME" not in json.dumps(episode.extra)


async def test_denied_classes_raw_bodies_and_bounds_never_persist_or_queue(
    client: httpx.AsyncClient,
) -> None:
    profile_key = f"deny-{uuid4().hex}"
    assert (
        await client.post("/v1/capture/profiles", json=_profile(profile_key))
    ).status_code == 201
    started = await _start(client, profile_key)
    url = f"/v1/capture/sessions/{started['id']}/events"
    marker = f"NEVER_STORE_{uuid4().hex}"
    with SessionLocal() as session:
        jobs_before = session.scalar(select(func.count()).select_from(Job))

    denied_class = await client.post(url, json=_event("raw_shell_output", content=marker))
    denied_raw = await client.post(url, json={**_event(content=marker), "body_kind": "raw"})
    denied_bound = await client.post(url, json=_event(content=marker * 10))
    assert [denied_class.status_code, denied_raw.status_code, denied_bound.status_code] == [
        403,
        403,
        403,
    ]

    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CaptureEvent)
                .where(CaptureEvent.session_id == UUID(started["id"]))
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Episode).where(Episode.content.contains(marker))
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(Job)) == jobs_before
        decisions = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "capture.policy.denied",
                AuditEvent.resource_id == started["id"],
            )
        ).all()
        assert len(decisions) == 3
        assert marker not in json.dumps([item.detail for item in decisions])


async def test_new_profile_version_governs_next_session_event(
    client: httpx.AsyncClient,
) -> None:
    profile_key = f"versioned-{uuid4().hex}"
    assert (
        await client.post("/v1/capture/profiles", json=_profile(profile_key))
    ).status_code == 201
    started = await _start(client, profile_key)
    url = f"/v1/capture/sessions/{started['id']}/events"
    first = await client.post(url, json=_event(sequence=1))
    assert first.status_code == 201
    assert first.json()["profile_version"] == 1

    version_two = _profile(
        profile_key,
        version=2,
        allowed_event_classes=["decision"],
        metadata_allowlists={"decision": []},
        event_bounds={"decision": {"max_content_chars": 100, "max_metadata_chars": 100}},
    )
    assert (await client.post("/v1/capture/profiles", json=version_two)).status_code == 201
    denied = await client.post(url, json=_event(sequence=2))
    assert denied.status_code == 403
    second = await client.post(url, json=_event("decision", sequence=2))
    assert second.status_code == 201
    assert second.json()["profile_version"] == 2

    with SessionLocal() as session:
        events = session.scalars(
            select(CaptureEvent)
            .where(CaptureEvent.session_id == UUID(started["id"]))
            .order_by(CaptureEvent.sequence)
        ).all()
        assert [(item.sequence, item.profile_version) for item in events] == [(1, 1), (2, 2)]
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(["capture.event.created", "capture.policy.denied"]),
                AuditEvent.resource_id.in_([started["id"], *(str(item.id) for item in events)]),
            )
        ).all()
        assert {item.detail["profile_version"] for item in audits} == {1, 2}
        assert all("content" not in item.detail for item in audits)


async def test_sensitivity_floor_intersects_credential_and_profile_is_tenant_scoped(
    client: httpx.AsyncClient, identity
) -> None:  # type: ignore[no-untyped-def]
    profile_key = f"restricted-{uuid4().hex}"
    restricted = _profile(profile_key, sensitivity_floor="restricted")
    assert (await client.post("/v1/capture/profiles", json=restricted)).status_code == 201
    started = await _start(client, profile_key)
    denied = await client.post(f"/v1/capture/sessions/{started['id']}/events", json=_event())
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "credential_capability_denied"

    _key, organization, workspace = identity
    restricted_key = create_principal_key(
        organization, workspace, "Restricted capture", ["capture.write", "memory.restricted"]
    )
    headers = {"Authorization": f"Bearer {restricted_key}"}
    capable = await _start(client, profile_key, headers=headers)
    allowed = await client.post(
        f"/v1/capture/sessions/{capable['id']}/events",
        json=_event(),
        headers=headers,
    )
    assert allowed.status_code == 201
    assert allowed.json()["sensitivity"] == "restricted"

    foreign_key, _foreign_org, _foreign_workspace = create_identity("Foreign policy")
    foreign_headers = {"Authorization": f"Bearer {foreign_key}"}
    assert (
        await client.get(f"/v1/capture/profiles/{profile_key}", headers=foreign_headers)
    ).status_code == 404
    foreign_start = await client.post(
        "/v1/capture/sessions",
        json={
            "client_session_id": f"foreign-{uuid4().hex}",
            "idempotency_key": f"foreign-{uuid4().hex}",
            "profile_key": profile_key,
        },
        headers=foreign_headers,
    )
    assert foreign_start.status_code == 404


@pytest.mark.parametrize(
    "event_class",
    [
        "raw_shell_output",
        "environment_variables",
        "clipboard",
        "file_body",
        "browser_body",
        "binary_data",
        "hidden_prompt",
        "transcript",
        "non_allowlisted_tool_body",
    ],
)
async def test_safe_default_denies_parent_disabled_classes(
    client: httpx.AsyncClient, event_class: str
) -> None:
    started = await _start(client, "safe-default")
    response = await client.post(
        f"/v1/capture/sessions/{started['id']}/events", json=_event(event_class)
    )
    assert response.status_code == 403
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CaptureEvent)
                .where(CaptureEvent.session_id == UUID(started["id"]))
            )
            == 0
        )

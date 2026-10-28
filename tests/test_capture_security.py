from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from albert.capture_retention import expire_capture_events
from albert.db import SessionLocal
from albert.models import AuditEvent, CaptureEvent, CaptureSession, Episode, Job, Memory, utcnow

pytestmark = pytest.mark.anyio


def _profile(profile_key: str, *, secret_policy: str, derivative_policy: str = "cascade") -> dict:
    return {
        "profile_key": profile_key,
        "version": 1,
        "allowed_event_classes": ["user_goal"],
        "metadata_allowlists": {"user_goal": ["note"]},
        "event_bounds": {"user_goal": {"max_content_chars": 1000, "max_metadata_chars": 1000}},
        "retention_days": 1,
        "secret_policy": secret_policy,
        "derivative_policy": derivative_policy,
    }


async def _start(client: httpx.AsyncClient, profile_key: str) -> dict:
    response = await client.post(
        "/v1/capture/sessions",
        json={
            "client_session_id": f"security-{uuid4().hex}",
            "idempotency_key": f"start-{uuid4().hex}",
            "profile_key": profile_key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _event(content: str, *, occurred_at: str | None = None) -> dict:
    value = {
        "sequence": 1,
        "idempotency_key": f"event-{uuid4().hex}",
        "event_class": "user_goal",
        "content": content,
        "metadata": {"note": content},
    }
    if occurred_at is not None:
        value["occurred_at"] = occurred_at
    return value


async def test_rejection_reports_only_stable_non_secret_metadata(client: httpx.AsyncClient) -> None:
    profile_key = f"reject-{uuid4().hex}"
    assert (
        await client.post(
            "/v1/capture/profiles", json=_profile(profile_key, secret_policy="reject")
        )
    ).status_code == 201
    started = await _start(client, profile_key)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"

    response = await client.post(
        f"/v1/capture/sessions/{started['id']}/events", json=_event(secret)
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "capture_secret_rejected",
        "reasons": ["github_token"],
    }
    assert secret not in response.text
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(CaptureEvent).where(CaptureEvent.session_id == UUID(started["id"]))
            )
            is None
        )
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "capture.secret.rejected",
                AuditEvent.resource_id == started["id"],
            )
        ).all()
        assert [item.detail["reasons"] for item in audits] == [["github_token"]]
        assert secret not in json.dumps([item.detail for item in audits])


async def test_redaction_precedes_every_persistent_and_queued_copy(
    client: httpx.AsyncClient,
) -> None:
    profile_key = f"redact-{uuid4().hex}"
    assert (
        await client.post(
            "/v1/capture/profiles", json=_profile(profile_key, secret_policy="redact")
        )
    ).status_code == 201
    started = await _start(client, profile_key)
    secret = "AKIAIOSFODNN7EXAMPLE"

    response = await client.post(
        f"/v1/capture/sessions/{started['id']}/events", json=_event(secret)
    )

    assert response.status_code == 201, response.text
    assert response.json()["screening_decision"] == "redacted"
    assert response.json()["screening_reasons"] == ["aws_access_key"]
    assert secret not in response.text
    with SessionLocal() as session:
        event = session.scalar(
            select(CaptureEvent).where(CaptureEvent.session_id == UUID(started["id"]))
        )
        assert event is not None
        episode = session.get(Episode, event.episode_id)
        jobs = session.scalars(
            select(Job).where(Job.organization_id == event.organization_id)
        ).all()
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.organization_id == event.organization_id)
        ).all()
        persisted = json.dumps(
            {
                "event": [event.content, event.metadata_],
                "episode": [episode.content, episode.extra] if episode else None,
                "jobs": [item.payload for item in jobs],
                "audits": [item.detail for item in audits],
            },
            default=str,
        )
        assert secret not in persisted
        assert persisted.count("[REDACTED:aws_access_key]") >= 2


@pytest.mark.parametrize("secret_policy", ["reject", "redact"])
async def test_nested_metadata_keys_are_screened(
    client: httpx.AsyncClient, secret_policy: str
) -> None:
    profile_key = f"metadata-key-{secret_policy}-{uuid4().hex}"
    assert (
        await client.post(
            "/v1/capture/profiles", json=_profile(profile_key, secret_policy=secret_policy)
        )
    ).status_code == 201
    started = await _start(client, profile_key)
    secret = "ghp_" + "A" * 36
    payload = _event("safe content")
    payload["metadata"] = {"note": {secret: "safe value"}}

    response = await client.post(
        f"/v1/capture/sessions/{started['id']}/events", json=payload
    )

    assert response.status_code == (422 if secret_policy == "reject" else 201), response.text
    assert secret not in response.text
    assert response.json()["detail" if secret_policy == "reject" else "screening_reasons"] == (
        {"error": "capture_secret_rejected", "reasons": ["github_token"]}
        if secret_policy == "reject"
        else ["github_token"]
    )
    with SessionLocal() as session:
        event = session.scalar(
            select(CaptureEvent).where(CaptureEvent.session_id == UUID(started["id"]))
        )
        if secret_policy == "reject":
            assert event is None
        else:
            assert event is not None
            episode = session.get(Episode, event.episode_id)
            persisted = json.dumps([event.metadata_, episode.extra if episode else None])
            assert secret not in persisted
            assert "[REDACTED:github_token]" in persisted


@pytest.mark.parametrize("secret_policy", ["reject", "redact"])
@pytest.mark.parametrize("target", ["session", "event"])
async def test_session_and_event_provenance_are_screened(
    client: httpx.AsyncClient, secret_policy: str, target: str
) -> None:
    profile_key = f"provenance-{target}-{secret_policy}-{uuid4().hex}"
    assert (
        await client.post(
            "/v1/capture/profiles", json=_profile(profile_key, secret_policy=secret_policy)
        )
    ).status_code == 201
    secret = "token=" + "B" * 24
    provenance = {"vcs": "git", "branch": secret}

    if target == "session":
        response = await client.post(
            "/v1/capture/sessions",
            json={
                "client_session_id": f"security-{uuid4().hex}",
                "idempotency_key": f"start-{uuid4().hex}",
                "profile_key": profile_key,
                "provenance": provenance,
            },
        )
    else:
        started = await _start(client, profile_key)
        payload = _event("safe content")
        payload["provenance"] = provenance
        response = await client.post(
            f"/v1/capture/sessions/{started['id']}/events", json=payload
        )

    assert response.status_code == (422 if secret_policy == "reject" else 201), response.text
    assert secret not in response.text
    if secret_policy == "reject":
        assert response.json()["detail"] == {
            "error": "capture_secret_rejected",
            "reasons": ["assigned_credential"],
        }
        return

    assert response.json()["provenance"]["branch"] == "[REDACTED:assigned_credential]"
    with SessionLocal() as session:
        if target == "session":
            capture_session = session.get(CaptureSession, UUID(response.json()["id"]))
            persisted = json.dumps(
                [
                    capture_session.provenance,
                    capture_session.branch_ref,
                ]
                if capture_session
                else None
            )
        else:
            event = session.get(CaptureEvent, UUID(response.json()["id"]))
            episode = session.get(Episode, event.episode_id) if event else None
            persisted = json.dumps(
                [event.provenance, event.branch_ref, episode.extra if episode else None]
                if event
                else None
            )
        assert secret not in persisted
        assert "[REDACTED:assigned_credential]" in persisted


@pytest.mark.parametrize(
    ("derivative_policy", "expected_status"),
    [("cascade", "deleted"), ("invalidate", "invalidated")],
)
async def test_retention_is_idempotent_and_provenance_aware(
    client: httpx.AsyncClient,
    derivative_policy: str,
    expected_status: str,
) -> None:
    profile_key = f"retention-{derivative_policy}-{uuid4().hex}"
    assert (
        await client.post(
            "/v1/capture/profiles",
            json=_profile(
                profile_key,
                secret_policy="redact",
                derivative_policy=derivative_policy,
            ),
        )
    ).status_code == 201
    started = await _start(client, profile_key)
    occurred_at = (utcnow() - timedelta(days=2)).isoformat()
    response = await client.post(
        f"/v1/capture/sessions/{started['id']}/events",
        json=_event("retained source", occurred_at=occurred_at),
    )
    assert response.status_code == 201, response.text

    with SessionLocal() as session:
        event = session.get(CaptureEvent, UUID(response.json()["id"]))
        assert event is not None
        episode = session.get(Episode, event.episode_id)
        assert episode is not None
        memory = Memory(
            episode_id=episode.id,
            organization_id=episode.organization_id,
            workspace_id=episode.workspace_id,
            owner_principal_id=episode.owner_principal_id,
            subject="derived",
            content=episode.content,
            sensitivity=episode.sensitivity,
        )
        session.add(memory)
        session.commit()

        assert expire_capture_events(session) == 1
        session.commit()
        assert expire_capture_events(session) == 0
        session.refresh(event)
        session.refresh(episode)
        session.refresh(memory)
        assert event.content == "" and event.metadata_ == {} and event.deleted_at is not None
        assert episode.content == "" and episode.extra == {} and episode.deleted_at is not None
        assert memory.content == "" and memory.status == expected_status

from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from albert.api import app
from albert.db import SessionLocal
from albert.models import AuditEvent, CaptureSession, Trace, utcnow
from albert.trace_writer import get_trace_writer

from .conftest import create_identity, create_principal_key, drain_jobs

pytestmark = pytest.mark.anyio


def _client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    )


async def _capture(client: httpx.AsyncClient, marker: str, adapter_version: str) -> tuple[str, str]:
    started = await client.post(
        "/v1/capture/sessions",
        json={
            "client_session_id": f"session-{uuid4().hex}",
            "idempotency_key": f"start-{uuid4().hex}",
            "provenance": {"adapter_version": adapter_version},
        },
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    event = await client.post(
        f"/v1/capture/sessions/{session_id}/events",
        json={
            "sequence": 1,
            "idempotency_key": f"event-{uuid4().hex}",
            "event_class": "decision",
            "content": f"Decision: retain {marker} only in the captured body",
        },
    )
    assert event.status_code == 201, event.text
    checkpoint = await client.post(
        f"/v1/capture/sessions/{session_id}/checkpoints",
        json={"through_sequence": 1, "idempotency_key": f"checkpoint-{uuid4().hex}"},
    )
    assert checkpoint.status_code == 201, checkpoint.text
    return session_id, checkpoint.json()["id"]


async def test_capture_health_is_body_free_bounded_and_tenant_isolated() -> None:
    key, organization, workspace = create_identity("Capture operations")
    foreign_key, foreign_org, _foreign_workspace = create_identity("Foreign capture operations")
    marker = f"private-body-{uuid4().hex}"
    foreign_marker = f"foreign-body-{uuid4().hex}"

    async with _client(key) as capture:
        session_id, checkpoint_id = await _capture(capture, marker, "adapter-00")
        for index in range(1, 12):
            response = await capture.post(
                "/v1/capture/sessions",
                json={
                    "client_session_id": f"version-{index}-{uuid4().hex}",
                    "idempotency_key": f"version-{index}-{uuid4().hex}",
                    "provenance": {"adapter_version": f"adapter-{index:02d}"},
                },
            )
            assert response.status_code == 201, response.text
        rejected = await capture.post(
            f"/v1/capture/sessions/{session_id}/events",
            json={
                "sequence": 2,
                "idempotency_key": f"rejected-{uuid4().hex}",
                "event_class": "decision",
                "content": "ghp_" + "A" * 36,
            },
        )
        assert rejected.status_code == 422
        injection = await capture.post(
            "/v1/context/session-start",
            json={"query": "capture operations health", "max_characters": 500},
        )
        assert injection.status_code == 200, injection.text

    async with _client(foreign_key) as foreign:
        foreign_session, foreign_checkpoint = await _capture(
            foreign, foreign_marker, "foreign-adapter"
        )

    drain_jobs()
    get_trace_writer().flush()
    with SessionLocal() as session:
        stalled = session.get(CaptureSession, UUID(session_id))
        assert stalled is not None
        stalled.updated_at = utcnow() - timedelta(minutes=10)
        session.commit()

    operator_key = create_principal_key(
        organization, workspace, "Body-free operator", ["console.read"]
    )
    async with _client(operator_key) as operator:
        response = await operator.get(
            "/v1/console/ops/capture", params={"hours": 24, "stalled_seconds": 60}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        encoded = json.dumps(body)
        assert body["failing_stage"] == "adapter"
        assert body["adapter"]["stalled_sessions"] >= 1
        assert len(body["adapter"]["adapter_versions"]) == 10
        assert body["ingestion"]["accepted"] >= 1
        assert body["ingestion"]["rejected"] >= 1
        assert body["consolidation"]["by_status"]["complete"] >= 1
        assert body["consolidation"]["last_successful_checkpoint"]["id"] == checkpoint_id
        assert body["injection"]["count"] >= 1
        assert body["telemetry"]["truncated"] is True
        assert marker not in encoded
        assert foreign_marker not in encoded
        assert foreign_session not in encoded
        assert foreign_checkpoint not in encoded

        forbidden = await operator.get(
            "/v1/console/ops/capture",
            params={"organization_id": str(foreign_org.id)},
        )
        assert forbidden.status_code == 404

    with SessionLocal() as session:
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.organization_id == organization.id)
        ).all()
    assert "capture.checkpoint.completed" in {item.event_type for item in audits}
    assert "console.ops.capture.read" in {item.event_type for item in audits}
    assert marker not in json.dumps([item.detail for item in audits])


async def test_capture_flight_recorder_reports_body_free_stage_progress() -> None:
    key, organization, _workspace = create_identity("Capture recorder")
    marker = f"trace-body-{uuid4().hex}"
    async with _client(key) as capture:
        await _capture(capture, marker, "recorder-adapter")
        injection = await capture.post(
            "/v1/context/session-start",
            json={"query": "recorder health", "max_characters": 500},
        )
        assert injection.status_code == 200, injection.text
    drain_jobs()
    get_trace_writer().flush()

    with SessionLocal() as session:
        traces = session.scalars(
            select(Trace)
            .where(Trace.organization_id == organization.id)
            .order_by(Trace.started_at.desc())
        ).all()
    summaries = [item.summary for item in traces]
    assert any(item.get("capture_stage") == "ingestion" for item in summaries)
    assert any(item.get("capture_stage") == "consolidation" for item in summaries)
    assert any(item.get("capture_stage") == "injection" for item in summaries)
    assert marker not in json.dumps(summaries)

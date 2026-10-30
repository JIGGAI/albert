from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from albert.capture_promotion import promote_capture_checkpoint
from albert.db import SessionLocal
from albert.models import CaptureCheckpoint, CaptureEvent, Job, Memory

from .conftest import create_identity, drain_jobs

pytestmark = pytest.mark.anyio


async def _capture(client: httpx.AsyncClient, events: list[tuple[str, str]]) -> UUID:
    started = await client.post(
        "/v1/capture/sessions",
        json={
            "client_session_id": f"promotion-{uuid4().hex}",
            "idempotency_key": f"promotion-{uuid4().hex}",
        },
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    for sequence, (event_class, content) in enumerate(events, 1):
        written = await client.post(
            f"/v1/capture/sessions/{session_id}/events",
            json={
                "sequence": sequence,
                "idempotency_key": f"event-{sequence}-{uuid4().hex}",
                "event_class": event_class,
                "content": content,
            },
        )
        assert written.status_code == 201, written.text
    checkpoint = await client.post(
        f"/v1/capture/sessions/{session_id}/checkpoints",
        json={"through_sequence": len(events), "idempotency_key": f"cp-{uuid4().hex}"},
    )
    assert checkpoint.status_code == 201, checkpoint.text
    assert checkpoint.json()["promotion_status"] == "pending"
    return UUID(checkpoint.json()["id"])


async def test_checkpoint_filters_deduplicates_types_and_supersedes(
    client: httpx.AsyncClient,
) -> None:
    checkpoint_id = await _capture(
        client,
        [
            ("user_goal", "Thanks, please keep working on this task."),
            ("decision", "Decision: the deployment region is us-east-1."),
            ("decision", "  decision: THE deployment region is us-east-1.  "),
            ("correction", "Correction: the deployment region is eu-west-1."),
            (
                "assistant_outcome",
                "Procedure: first run the migration, then verify the health endpoint.",
            ),
        ],
    )
    drain_jobs()

    status = await client.get(f"/v1/capture/checkpoints/{checkpoint_id}")
    assert status.status_code == 200
    assert status.json()["promotion_status"] == "complete"
    assert status.json()["promotion_error"] is None

    with SessionLocal() as session:
        checkpoint = session.get(CaptureCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.promotion_status == "complete"
        assert checkpoint.promotion_error is None
        assert checkpoint.metadata_["promoted_count"] == 3
        events = list(
            session.scalars(
                select(CaptureEvent)
                .where(CaptureEvent.session_id == checkpoint.session_id)
                .order_by(CaptureEvent.sequence)
            )
        )
        assert events[0].derived_memory_id is None
        assert events[1].derived_memory_id == events[2].derived_memory_id
        memories = list(
            session.scalars(
                select(Memory).where(
                    Memory.organization_id == checkpoint.organization_id,
                    Memory.metadata_["automatic_capture_promotion"].as_boolean().is_(True),
                )
            )
        )
        by_type = {memory.memory_type: memory for memory in memories}
        assert {"decision", "fact", "procedure"} <= set(by_type)
        assert by_type["decision"].status == "superseded"
        assert by_type["decision"].valid_until == events[3].occurred_at
        assert by_type["fact"].metadata_["supersedes_memory_id"] == str(
            by_type["decision"].id
        )
        assert by_type["procedure"].metadata_["capture_source_trust"] == "untrusted"
        assert by_type["procedure"].metadata_["capture_event_id"] == str(events[4].id)

        active_before = session.scalar(
            select(Job).where(Job.id == checkpoint.promotion_job_id)
        ).status
        assert active_before == "complete"
        promote_capture_checkpoint(session, checkpoint.id)
        assert checkpoint.promotion_status == "complete"


async def test_same_content_isolated_by_tenant() -> None:
    identity_a = create_identity("promotion-a")
    identity_b = create_identity("promotion-b")
    checkpoint_ids: list[UUID] = []
    for key, _organization, _workspace in (identity_a, identity_b):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=__import__("albert.api", fromlist=["app"]).app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {key}"},
        ) as tenant_client:
            checkpoint_ids.append(
                await _capture(
                    tenant_client,
                    [("decision", "Decision: use exactly one tenant-local canonical memory.")],
                )
            )
    drain_jobs()
    with SessionLocal() as session:
        checkpoints = [session.get(CaptureCheckpoint, value) for value in checkpoint_ids]
        assert all(
            value is not None and value.promotion_status == "complete" for value in checkpoints
        )
        assert checkpoints[0].organization_id != checkpoints[1].organization_id  # type: ignore[union-attr]
        assert checkpoints[0].derived_memory_id != checkpoints[1].derived_memory_id  # type: ignore[union-attr]

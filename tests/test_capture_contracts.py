from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from albert.db import SessionLocal
from albert.models import CaptureCheckpoint, CaptureEvent, Episode

from .conftest import create_identity, create_principal_key

pytestmark = pytest.mark.anyio


async def _start(client: httpx.AsyncClient, *, key: str = "session-1") -> dict:
    response = await client.post(
        "/v1/capture/sessions",
        json={"client_session_id": "client-session-1", "idempotency_key": key},
    )
    assert response.status_code == 201
    return response.json()


def _event(sequence: int, *, key: str | None = None, event_class: str = "user_goal") -> dict:
    return {
        "sequence": sequence,
        "idempotency_key": key or f"event-{sequence}",
        "event_class": event_class,
        "content": f"Captured event {sequence}",
        "metadata": {"safe": True},
    }


async def test_capture_lifecycle_is_ordered_idempotent_and_linked(
    client: httpx.AsyncClient,
) -> None:
    started = await _start(client)
    repeated_start = await client.post(
        "/v1/capture/sessions",
        json={"client_session_id": "client-session-1", "idempotency_key": "session-1"},
    )
    assert repeated_start.status_code == 200
    assert repeated_start.json() == started

    event_url = f"/v1/capture/sessions/{started['id']}/events"
    first = await client.post(event_url, json=_event(1))
    repeated = await client.post(event_url, json=_event(1))
    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json() == first.json()

    out_of_order = await client.post(event_url, json=_event(3))
    assert out_of_order.status_code == 409
    assert out_of_order.json()["detail"]["expected_sequence"] == 2

    batch = await client.post(
        f"{event_url}/batch",
        json={"events": [_event(2), _event(3)]},
    )
    assert batch.status_code == 201
    assert [item["sequence"] for item in batch.json()["events"]] == [2, 3]
    repeated_batch = await client.post(
        f"{event_url}/batch",
        json={"events": [_event(2), _event(3)]},
    )
    assert repeated_batch.status_code == 200
    assert repeated_batch.json() == batch.json()

    checkpoint = await client.post(
        f"/v1/capture/sessions/{started['id']}/checkpoints",
        json={"through_sequence": 3, "idempotency_key": "checkpoint-3"},
    )
    assert checkpoint.status_code == 201
    assert checkpoint.json()["episode_id"] == batch.json()["events"][-1]["episode_id"]
    repeated_checkpoint = await client.post(
        f"/v1/capture/sessions/{started['id']}/checkpoints",
        json={"through_sequence": 3, "idempotency_key": "checkpoint-3"},
    )
    assert repeated_checkpoint.status_code == 200
    assert repeated_checkpoint.json() == checkpoint.json()

    ended = await client.post(
        f"/v1/capture/sessions/{started['id']}/end",
        json={"idempotency_key": "end-3", "final_sequence": 3},
    )
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    repeated_end = await client.post(
        f"/v1/capture/sessions/{started['id']}/end",
        json={"idempotency_key": "end-3", "final_sequence": 3},
    )
    assert repeated_end.status_code == 200
    assert repeated_end.json() == ended.json()
    assert (await client.post(event_url, json=_event(4))).status_code == 409

    with SessionLocal() as session:
        stored = session.scalars(
            select(CaptureEvent).where(CaptureEvent.session_id == UUID(started["id"]))
        ).all()
        assert [item.sequence for item in stored] == [1, 2, 3]
        assert len({item.episode_id for item in stored}) == 3
        assert all(session.get(Episode, item.episode_id) is not None for item in stored)
        saved_checkpoint = session.scalar(
            select(CaptureCheckpoint).where(
                CaptureCheckpoint.session_id == UUID(started["id"])
            )
        )
        assert saved_checkpoint is not None
        assert saved_checkpoint.episode_id == stored[-1].episode_id


async def test_invalid_batch_and_reused_keys_write_nothing(client: httpx.AsyncClient) -> None:
    started = await _start(client, key="atomic-session")
    event_url = f"/v1/capture/sessions/{started['id']}/events"
    invalid = await client.post(
        f"{event_url}/batch",
        json={"events": [_event(1), _event(3)]},
    )
    assert invalid.status_code == 409
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(CaptureEvent).where(
                CaptureEvent.session_id == UUID(started["id"])
            )
        ) == 0

    created = await client.post(event_url, json=_event(1, key="stable-key"))
    assert created.status_code == 201
    changed = await client.post(
        event_url,
        json={**_event(1, key="stable-key"), "content": "different content"},
    )
    assert changed.status_code == 409

    too_large = await client.post(
        f"{event_url}/batch",
        json={"events": [_event(number) for number in range(2, 103)]},
    )
    assert too_large.status_code == 422
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(CaptureEvent).where(
                CaptureEvent.session_id == UUID(started["id"])
            )
        ) == 1


async def test_concurrent_event_retry_stores_one_canonical_result(
    client: httpx.AsyncClient,
) -> None:
    started = await _start(client, key="concurrent-session")
    event_url = f"/v1/capture/sessions/{started['id']}/events"
    first, second = await asyncio.gather(
        client.post(event_url, json=_event(1, key="concurrent-event")),
        client.post(event_url, json=_event(1, key="concurrent-event")),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["id"] == second.json()["id"]
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(CaptureEvent).where(
                CaptureEvent.session_id == UUID(started["id"])
            )
        ) == 1


async def test_concurrent_checkpoint_retry_stores_one_canonical_result(
    client: httpx.AsyncClient,
) -> None:
    started = await _start(client, key="concurrent-checkpoint-session")
    event_url = f"/v1/capture/sessions/{started['id']}/events"
    assert (await client.post(event_url, json=_event(1))).status_code == 201
    checkpoint_url = f"/v1/capture/sessions/{started['id']}/checkpoints"
    checkpoint = {"through_sequence": 1, "idempotency_key": "concurrent-checkpoint"}

    first, second = await asyncio.gather(
        client.post(checkpoint_url, json=checkpoint),
        client.post(checkpoint_url, json=checkpoint),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["id"] == second.json()["id"]
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(CaptureCheckpoint).where(
                CaptureCheckpoint.session_id == UUID(started["id"])
            )
        ) == 1


async def test_capture_versions_capabilities_and_identifiers_do_not_leak(
    client: httpx.AsyncClient, identity
) -> None:  # type: ignore[no-untyped-def]
    unsupported = await client.post(
        "/v1/capture/sessions",
        json={"version": "99", "client_session_id": "future", "idempotency_key": "future"},
    )
    assert unsupported.status_code == 426
    assert unsupported.json()["detail"]["supported_versions"] == ["1"]

    started = await _start(client, key="security-session")
    foreign_key, _foreign_org, _foreign_workspace = create_identity("Foreign capture")
    _key, organization, workspace = identity
    sibling_key = create_principal_key(
        organization, workspace, "Sibling capture", ["capture.write"]
    )
    no_system_key = create_principal_key(
        organization, workspace, "Ordinary capture", ["capture.write"]
    )
    session_url = f"/v1/capture/sessions/{started['id']}/events"
    unknown_url = f"/v1/capture/sessions/{uuid4()}/events"
    for key in (foreign_key, sibling_key):
        headers = {"Authorization": f"Bearer {key}"}
        known = await client.post(session_url, json=_event(1), headers=headers)
        unknown = await client.post(unknown_url, json=_event(1), headers=headers)
        assert known.status_code == unknown.status_code == 404
        assert known.json() == unknown.json()

    denied = await client.post(
        session_url,
        json=_event(1, event_class="system"),
        headers={"Authorization": f"Bearer {no_system_key}"},
    )
    assert denied.status_code == 403
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(CaptureEvent).where(
                CaptureEvent.session_id == UUID(started["id"])
            )
        ) == 0

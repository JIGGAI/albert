from __future__ import annotations

import threading
import time
from uuid import UUID

import httpx
import pytest

import albert.worker as worker
from albert.api import _preferred_wait, app
from albert.db import SessionLocal
from albert.models import Job
from albert.services import MAX_JOB_ATTEMPTS

from .conftest import drain_jobs

pytestmark = pytest.mark.anyio


def _process_one() -> None:
    with SessionLocal() as session:
        job = worker.claim_job(session, "test-worker")
        assert job is not None
        worker.process_job(session, job)


async def test_new_episode_reports_queued_then_complete(client: httpx.AsyncClient) -> None:
    drain_jobs()
    created = await client.post("/v1/episodes", json={"content": "Orion ships on Fridays."})
    assert created.status_code == 201
    body = created.json()
    assert body["indexing"]["state"] == "queued"
    assert body["indexing"]["searchable"] is False
    assert body["indexing"]["max_attempts"] == MAX_JOB_ATTEMPTS
    assert created.headers["location"] == f"/v1/episodes/{body['id']}"
    assert created.headers["retry-after"] == "1"

    drain_jobs()
    read = await client.get(f"/v1/episodes/{body['id']}")
    indexing = read.json()["indexing"]
    assert indexing["state"] == "complete" and indexing["searchable"] is True
    assert UUID(indexing["memory_id"])
    assert "retry-after" not in read.headers
    memory = await client.get(f"/v1/memories/{indexing['memory_id']}")
    assert memory.status_code == 200


async def test_prefer_wait_returns_once_searchable(client: httpx.AsyncClient) -> None:
    drain_jobs()
    background = threading.Timer(0.4, drain_jobs)
    background.start()
    try:
        started = time.monotonic()
        created = await client.post(
            "/v1/episodes",
            json={"content": "Vega reviews the release notes."},
            headers={"Prefer": "wait=10"},
        )
    finally:
        background.join()
    assert created.status_code == 201
    assert created.json()["indexing"]["state"] == "complete"
    assert created.headers["preference-applied"] == "wait=10"
    assert "retry-after" not in created.headers
    assert time.monotonic() - started < 10

    search = await client.post("/v1/search", json={"query": "Vega release notes"})
    memory_id = created.json()["indexing"]["memory_id"]
    assert any(hit["memory_id"] == memory_id for hit in search.json()["hits"])


async def test_prefer_wait_gives_up_after_the_requested_time(client: httpx.AsyncClient) -> None:
    drain_jobs()
    started = time.monotonic()
    created = await client.post(
        "/v1/episodes", json={"content": "Nobody indexes this."}, headers={"Prefer": "wait=0.6"}
    )
    elapsed = time.monotonic() - started
    assert 0.5 <= elapsed < 5
    assert created.json()["indexing"]["state"] == "queued"
    assert created.headers["retry-after"] == "1"
    drain_jobs()


async def test_wait_is_capped_by_configuration(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/episodes", json={"content": "Capped wait probe."})
    drain_jobs()
    read = await client.get(f"/v1/episodes/{created.json()['id']}", headers={"Prefer": "wait=999"})
    assert read.headers["preference-applied"] == "wait=30"
    assert read.json()["indexing"]["state"] == "complete"


async def test_retrying_and_failed_states_carry_the_error(
    client: httpx.AsyncClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    drain_jobs()

    def model_timeout(_session, _episode_id) -> None:  # type: ignore[no-untyped-def]
        raise TimeoutError("model call exceeded 30s")

    monkeypatch.setattr(worker, "enrich_episode", model_timeout)
    created = await client.post("/v1/episodes", json={"content": "Flaky model probe."})
    episode_id = created.json()["id"]
    _process_one()

    retrying = await client.get(f"/v1/episodes/{episode_id}")
    indexing = retrying.json()["indexing"]
    assert indexing["state"] == "retrying"
    assert indexing["attempt"] == 1
    assert indexing["last_error"] == "TimeoutError: model call exceeded 30s"
    assert indexing["next_attempt_at"] is not None
    assert int(retrying.headers["retry-after"]) >= 1

    with SessionLocal() as session:
        job = session.get(Job, UUID(_job_id(episode_id)))
        job.attempts = MAX_JOB_ATTEMPTS - 1
        job.available_at = job.created_at
        session.commit()
    _process_one()

    failed = (await client.get(f"/v1/episodes/{episode_id}")).json()
    assert failed["enrichment_status"] == "failed"
    assert failed["indexing"]["state"] == "failed"
    assert failed["indexing"]["last_error"].startswith("TimeoutError")


def _job_id(episode_id: str) -> str:
    from albert.models import Episode

    with SessionLocal() as session:
        episode = session.get(Episode, UUID(episode_id))
        assert episode is not None and episode.enrichment_job_id is not None
        return str(episode.enrichment_job_id)


def test_prefer_header_parsing() -> None:
    assert _preferred_wait(None) is None
    assert _preferred_wait("return=minimal") is None
    assert _preferred_wait("return=minimal, wait=5") == 5
    assert _preferred_wait('wait="2.5"') == 2.5
    assert _preferred_wait("wait=abc") is None
    assert _preferred_wait("wait=-1") is None
    assert _preferred_wait("wait=nan") is None
    assert _preferred_wait("wait=600") == 30


async def test_mcp_ingest_forwards_wait(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import albert.mcp_server as mcp_server

    seen: dict = {}

    async def fake_request(method, path, payload=None, headers=None):  # type: ignore[no-untyped-def]
        seen.update(method=method, path=path, headers=headers)
        return {}

    monkeypatch.setattr(mcp_server, "_request", fake_request)
    await mcp_server.memory_ingest("probe", wait_seconds=12)
    assert seen["headers"] == {"Prefer": "wait=12"}
    await mcp_server.memory_ingest("probe")
    assert seen["headers"] is None


async def test_repost_with_wait_needs_only_write(identity) -> None:  # type: ignore[no-untyped-def]
    """Albert Cloud's ingestion key is write-only; re-posting is how it waits."""
    from .conftest import create_principal_key

    _key, organization, workspace = identity
    writer = create_principal_key(organization, workspace, "Ingestion", ["memory.write"])
    body = {"content": "Write-only wait probe.", "source_uri": "albert-cloud://documents/j/0"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {writer}"},
    ) as client:
        drain_jobs()
        first = await client.post("/v1/episodes", json=body)
        assert first.status_code == 201
        assert (await client.get(f"/v1/episodes/{first.json()['id']}")).status_code == 403
        background = threading.Timer(0.3, drain_jobs)
        background.start()
        try:
            again = await client.post("/v1/episodes", json=body, headers={"Prefer": "wait=10"})
        finally:
            background.join()
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["indexing"]["state"] == "complete"

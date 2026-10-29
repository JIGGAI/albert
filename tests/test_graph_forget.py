"""Forgetting reaches a graph builder's own store, not only Albert's.

Graphiti keeps its own graph of episodes, facts and entity summaries. Deleting a
memory or episode, retention expiry and workspace deletion must queue its
removal there, and a build that finishes after the memory was forgotten must not
leave anything behind.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from albert.classifier import ExtractedEntity, ExtractedRelationship
from albert.db import SessionLocal
from albert.graph_builders import (
    BuildRequest,
    BuiltRelationship,
    graphiti_episode_id,
    set_graph_builder,
)
from albert.models import Job, Memory

from .conftest import drain_jobs
from .test_shared_memories import _client, _store, _tenant

pytestmark = pytest.mark.anyio


class OwnStoreBuilder:
    """A builder with a store of its own, recording what it is asked to forget."""

    name = "own-store"
    version = "test"
    keeps_own_store = True

    def __init__(self) -> None:
        self.built: list[BuildRequest] = []
        self.forgotten: list[tuple[UUID, UUID | None, list[UUID], list[str]]] = []
        self.scopes: list[tuple[UUID, UUID | None]] = []
        self.during_build = None

    def build(self, request: BuildRequest, fallback: list) -> list[BuiltRelationship]:  # type: ignore[type-arg]
        del fallback
        self.built.append(request)
        if self.during_build is not None:
            self.during_build(request)
        episode = graphiti_episode_id(request.memory_id, request.content_fingerprint)
        return [
            BuiltRelationship(
                valid_from=request.occurred_at,
                relationship=ExtractedRelationship(
                    source=ExtractedEntity("Heron Service", "service"),
                    relation_type="uses",
                    target=ExtractedEntity("Heron Queue", "service"),
                ),
                metadata={"builder": "graphiti", "graphiti_episode_id": episode},
            )
        ]

    def forget(self, organization_id, workspace_id, source_ids, episode_ids) -> int:  # type: ignore[no-untyped-def]
        self.forgotten.append((organization_id, workspace_id, source_ids, episode_ids))
        return len(episode_ids)

    def forget_scope(self, organization_id, workspace_id) -> None:  # type: ignore[no-untyped-def]
        self.scopes.append((organization_id, workspace_id))

    def readiness(self) -> str:
        return "test"


@pytest.fixture()
def builder():  # type: ignore[no-untyped-def]
    instance = OwnStoreBuilder()
    set_graph_builder(instance)
    yield instance
    set_graph_builder(None)


async def test_deleting_a_memory_forgets_the_builders_copy(builder) -> None:  # type: ignore[no-untyped-def]
    _org_key, organization, home, _sibling, home_key, _sibling_key = _tenant()
    memory = (await _store(home_key, "Heron service writes to the Heron queue")).json()
    drain_jobs()
    built = builder.built[-1]
    episode = graphiti_episode_id(built.memory_id, built.content_fingerprint)

    async with _client(home_key) as client:
        assert (await client.delete(f"/v1/memories/{memory['id']}")).status_code == 204
    drain_jobs()

    assert builder.forgotten, "no forget job ran"
    organization_id, workspace_id, sources, episodes = builder.forgotten[-1]
    assert organization_id == organization.id and workspace_id == home.id
    assert UUID(memory["id"]) in sources
    # The current revision's episode, and the one recorded on its edges.
    assert episode in episodes


async def test_deleting_an_episode_forgets_the_episode_and_its_memory(builder) -> None:  # type: ignore[no-untyped-def]
    _org_key, _organization, _home, _sibling, home_key, _sibling_key = _tenant()
    async with _client(home_key) as client:
        posted = await client.post(
            "/v1/episodes", json={"content": "Heron standup: the queue migration slips"}
        )
        assert posted.status_code in (200, 201, 202), posted.text
        episode_id = UUID(posted.json()["id"])
        drain_jobs()
        assert (await client.delete(f"/v1/episodes/{episode_id}")).status_code == 204
    drain_jobs()
    sources = {source for _o, _w, ids, _e in builder.forgotten for source in ids}
    assert episode_id in sources


async def test_a_build_that_outlives_its_memory_is_forgotten(builder) -> None:  # type: ignore[no-untyped-def]
    _org_key, _organization, _home, _sibling, home_key, _sibling_key = _tenant()

    def forget_mid_build(request: BuildRequest) -> None:
        with SessionLocal() as other:
            from albert.models import utcnow
            from albert.services import _scrub_memory

            memory = other.get(Memory, request.memory_id)
            assert memory is not None
            _scrub_memory(other, memory, utcnow())
            other.commit()

    builder.during_build = forget_mid_build
    memory = (await _store(home_key, "Heron cache is warmed at boot")).json()
    drain_jobs()
    built = builder.built[-1]
    late = graphiti_episode_id(built.memory_id, built.content_fingerprint)
    # Once when it was forgotten, and again for what the late build stored.
    assert sum(late in episodes for *_rest, episodes in builder.forgotten) == 2
    with SessionLocal() as session:
        row = session.get(Memory, UUID(memory["id"]))
        assert row is not None and row.status == "deleted"


async def test_deleting_a_workspace_drops_the_builders_scope(builder) -> None:  # type: ignore[no-untyped-def]
    from .test_delete_workspace import _delete, _manager

    _org_key, organization, _home, sibling, _home_key, sibling_key = _tenant()
    await _store(sibling_key, "Heron dashboard lives in the sibling workspace")
    drain_jobs()
    deleted = await _delete(_manager(organization), sibling, memories="organization")
    assert deleted.status_code == 200, deleted.text
    drain_jobs()
    assert (organization.id, sibling.id) in builder.scopes


async def test_a_builder_without_its_own_store_queues_nothing(client: httpx.AsyncClient) -> None:
    memory = (await client.post("/v1/memories", json={"content": "Plain regex memory"})).json()
    drain_jobs()
    assert (await client.delete(f"/v1/memories/{memory['id']}")).status_code == 204
    with SessionLocal() as session:
        kinds = set(session.scalars(select(Job.job_type).where(Job.status == "pending")))
    assert "graph_forget" not in kinds


async def test_capture_retention_forgets_the_builders_copy(  # type: ignore[no-untyped-def]
    client: httpx.AsyncClient, builder
) -> None:
    from datetime import timedelta
    from uuid import uuid4

    from albert.capture_retention import expire_capture_events
    from albert.models import CaptureEvent, utcnow

    from .test_capture_security import _event, _profile, _start

    profile_key = f"retention-forget-{uuid4().hex}"
    created = await client.post(
        "/v1/capture/profiles", json=_profile(profile_key, secret_policy="redact")
    )
    assert created.status_code == 201, created.text
    started = await _start(client, profile_key)
    response = await client.post(
        f"/v1/capture/sessions/{started['id']}/events",
        json=_event("Heron capture", occurred_at=(utcnow() - timedelta(days=2)).isoformat()),
    )
    assert response.status_code == 201, response.text
    with SessionLocal() as session:
        event = session.get(CaptureEvent, UUID(response.json()["id"]))
        assert event is not None and event.episode_id is not None
        episode_id = event.episode_id
        assert expire_capture_events(session) == 1
        session.commit()
    drain_jobs()
    sources = {source for _o, _w, ids, _e in builder.forgotten for source in ids}
    assert episode_id in sources

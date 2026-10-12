from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.helpers import validate_group_id

from albert.config import Settings
from albert.graph_builders import BuildRequest, GraphitiGraphBuilder

FALKORDB_URL = os.environ.get("ALBERT_TEST_FALKORDB_URL")


class TestGraphitiBuilder(GraphitiGraphBuilder):
    __test__ = False

    def _episode_type(self) -> str:
        return "text"


class FakeGraphiti:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.episode_contents: list[str] = []
        self.closed = False
        self.episodes: dict[str, object] = {}
        self.driver = SimpleNamespace(_init_task=None)
        self.nodes = SimpleNamespace(episode=FakeEpisodeNodes(self.episodes))

    async def retrieve_episodes(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return list(self.episodes.values())

    async def add_episode(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        episode = self.episodes[kwargs["uuid"]]
        self.episode_contents.append(episode.content)  # type: ignore[attr-defined]
        episode.content = ""  # type: ignore[attr-defined]
        source = SimpleNamespace(uuid="source", name="Albert", labels=["Entity", "Product"])
        target = SimpleNamespace(uuid="target", name="FalkorDB", labels=["Entity", "Database"])
        edge = SimpleNamespace(
            uuid="edge-1",
            source_node_uuid="source",
            target_node_uuid="target",
            name="RUNS ON",
            episodes=[kwargs["uuid"]],
            attributes={"confidence": 0.91},
            valid_at=datetime(2026, 1, 1, tzinfo=UTC),
            invalid_at=datetime(2026, 2, 1, tzinfo=UTC),
            reference_time=None,
        )
        return SimpleNamespace(nodes=[source, target], edges=[edge])

    async def close(self) -> None:
        self.closed = True


class FakeEpisodeNodes:
    def __init__(self, episodes: dict[str, object]) -> None:
        self.episodes = episodes

    async def get_by_uuid(self, episode_uuid: str) -> object:
        try:
            return self.episodes[episode_uuid]
        except KeyError as exc:
            raise NodeNotFoundError(episode_uuid) from exc

    async def save(self, episode: object) -> object:
        self.episodes[episode.uuid] = episode  # type: ignore[attr-defined]
        return episode


def test_graphiti_maps_temporal_facts_and_scopes_episode() -> None:
    fake = FakeGraphiti()
    builder = TestGraphitiBuilder(SimpleNamespace())  # type: ignore[arg-type]
    builder._client = lambda: fake  # type: ignore[method-assign]
    organization_id = uuid4()
    workspace_id = uuid4()
    memory_id = uuid4()
    built = builder.build(
        BuildRequest(
            memory_id=memory_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            subject="Runtime",
            content="Albert runs on FalkorDB.",
            occurred_at=datetime(2025, 12, 1, tzinfo=UTC),
            content_fingerprint="revision-1",
        ),
        [],
    )

    assert fake.closed is True
    group_id = fake.calls[0]["group_id"]
    assert group_id == f"org_{organization_id}_workspace_{workspace_id}"
    assert validate_group_id(group_id) is True
    assert fake.calls[0]["uuid"]
    assert fake.calls[0]["previous_episode_uuids"] == []
    assert len(built) == 1
    fact = built[0]
    assert fact.relationship.source.name == "Albert"
    assert fact.relationship.source.entity_type == "product"
    assert fact.relationship.relation_type == "runs_on"
    assert fact.relationship.target.name == "FalkorDB"
    assert fact.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert fact.valid_until == datetime(2026, 2, 1, tzinfo=UTC)
    assert fact.metadata["builder"] == "graphiti"


def test_graphiti_episode_identity_is_stable_for_retries_and_changes_for_edits() -> None:
    fake = FakeGraphiti()
    builder = TestGraphitiBuilder(SimpleNamespace())  # type: ignore[arg-type]
    builder._client = lambda: fake  # type: ignore[method-assign]
    base = {
        "memory_id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": None,
        "subject": "Runtime",
        "content": "Albert runs on FalkorDB.",
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    builder.build(BuildRequest(**base, content_fingerprint="one"), [])
    builder.build(BuildRequest(**base, content_fingerprint="one"), [])
    builder.build(BuildRequest(**base, content_fingerprint="two"), [])

    ids = [call["uuid"] for call in fake.calls]
    assert ids[0] == ids[1]
    assert ids[1] != ids[2]
    assert fake.episode_contents == [base["content"], base["content"], base["content"]]
    assert fake.calls[1]["previous_episode_uuids"] == []
    assert fake.calls[2]["previous_episode_uuids"] == [ids[0]]
    group_id = fake.calls[0]["group_id"]
    assert group_id == f"org_{base['organization_id']}_workspace_global"
    assert validate_group_id(group_id) is True


def test_graphiti_first_write_upserts_episode_in_real_falkordb() -> None:
    if not FALKORDB_URL:
        pytest.skip("ALBERT_TEST_FALKORDB_URL is not set")
    settings = Settings(
        graph_builder="graphiti",
        graph_store="falkordb",
        falkordb_url=FALKORDB_URL,
        falkordb_graph=f"graphiti_first_write_{uuid4().hex}",
        llm_provider="openai-compatible",
        embedding_provider="openai-compatible",
        openai_api_key="test-only-placeholder",
        classification_model="test-model",
    )
    builder = GraphitiGraphBuilder(settings)
    request = BuildRequest(
        memory_id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        subject="First write regression",
        content="Albert uses FalkorDB.",
        occurred_at=datetime.now(UTC),
        content_fingerprint="revision-1",
    )
    episode_uuid = str(uuid4())
    group_id = f"org_{request.organization_id}_workspace_{request.workspace_id}"
    async def verify() -> None:
        client = builder._client()
        try:
            first_previous = await builder._prepare_episode(
                client, request, episode_uuid, group_id
            )
            stored = await client.nodes.episode.get_by_uuid(episode_uuid)
            retry_previous = await builder._prepare_episode(
                client, request, episode_uuid, group_id
            )
            assert stored.uuid == episode_uuid
            assert first_previous == []
            assert retry_previous == []
        finally:
            await builder._settle_background_tasks()
            await client.close()

    asyncio.run(verify())

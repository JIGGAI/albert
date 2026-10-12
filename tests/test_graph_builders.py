from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.helpers import validate_group_id

from albert.config import Settings
from albert.graph_builders import BuildRequest, GraphitiGraphBuilder

FALKORDB_URL = os.environ.get("ALBERT_TEST_FALKORDB_URL")
GRAPHITI_API_KEY = os.environ.get("ALBERT_TEST_GRAPHITI_API_KEY")
GRAPHITI_BASE_URL = os.environ.get("ALBERT_TEST_GRAPHITI_BASE_URL")
GRAPHITI_CHAT_MODEL = os.environ.get("ALBERT_TEST_GRAPHITI_CHAT_MODEL")
GRAPHITI_EMBEDDING_MODEL = os.environ.get("ALBERT_TEST_GRAPHITI_EMBEDDING_MODEL")


class TestGraphitiBuilder(GraphitiGraphBuilder):
    __test__ = False

    def _episode_type(self) -> str:
        return "text"

    @staticmethod
    async def _get_episode(driver, episode_uuid):  # type: ignore[no-untyped-def]
        try:
            return driver.episodes[episode_uuid]
        except KeyError as exc:
            raise NodeNotFoundError(episode_uuid) from exc

    @staticmethod
    async def _save_episode(driver, episode):  # type: ignore[no-untyped-def]
        driver.episodes[episode.uuid] = episode


class FakeGraphiti:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.episode_contents: list[str] = []
        self.closed = False
        self.episodes: dict[str, object] = {}
        self.driver = SimpleNamespace(_init_task=None)
        self.scoped_driver = SimpleNamespace(episodes=self.episodes)
        self.retrieve_drivers: list[object] = []

    def _resolve_request_scope(self, group_id):  # type: ignore[no-untyped-def]
        return group_id, self.scoped_driver, SimpleNamespace(driver=self.scoped_driver)

    async def retrieve_episodes(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args
        self.retrieve_drivers.append(kwargs["driver"])
        return list(kwargs["driver"].episodes.values())

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
    assert fake.retrieve_drivers == [fake.scoped_driver]
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


def test_graphiti_surfaces_scoped_driver_background_failures() -> None:
    async def verify() -> None:
        async def fail_initialization() -> None:
            raise RuntimeError("scoped index initialization failed")

        task = asyncio.create_task(fail_initialization())
        scoped_driver = SimpleNamespace(_init_task=task)
        client = SimpleNamespace(
            driver=SimpleNamespace(_albert_scoped_drivers={"scope": scoped_driver})
        )

        with pytest.raises(RuntimeError, match="scoped index initialization failed"):
            await GraphitiGraphBuilder._settle_background_tasks(client)

    asyncio.run(verify())


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
        from graphiti_core.nodes import EpisodicNode

        client = builder._client()
        try:
            first_previous = await builder._prepare_episode(
                client, request, episode_uuid, group_id
            )
            _, driver, _ = client._resolve_request_scope(group_id)
            stored = await EpisodicNode.get_by_uuid(driver, episode_uuid)
            retry_previous = await builder._prepare_episode(
                client, request, episode_uuid, group_id
            )
            assert stored.uuid == episode_uuid
            assert first_previous == []
            assert retry_previous == []
        finally:
            await builder._settle_background_tasks(client)
            await client.close()

    asyncio.run(verify())


def test_graphiti_live_builder_first_write_and_retry() -> None:
    required = (
        FALKORDB_URL,
        GRAPHITI_API_KEY,
        GRAPHITI_BASE_URL,
        GRAPHITI_CHAT_MODEL,
        GRAPHITI_EMBEDDING_MODEL,
    )
    if not all(required):
        pytest.skip("live FalkorDB and Graphiti model test settings are not set")
    settings = Settings(
        graph_builder="graphiti",
        graph_store="falkordb",
        falkordb_url=FALKORDB_URL,
        falkordb_graph=f"graphiti_live_build_{uuid4().hex}",
        llm_provider="openai-compatible",
        embedding_provider="openai-compatible",
        openai_base_url=GRAPHITI_BASE_URL,
        openai_api_key=GRAPHITI_API_KEY,
        classification_model=GRAPHITI_CHAT_MODEL,
        embedding_model=GRAPHITI_EMBEDDING_MODEL,
        embedding_dimensions=int(
            os.environ.get("ALBERT_TEST_GRAPHITI_EMBEDDING_DIMENSIONS", "384")
        ),
    )
    builder = GraphitiGraphBuilder(settings)
    request = BuildRequest(
        memory_id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        subject="Live first write regression",
        content="Albert uses FalkorDB for temporal graph memory.",
        occurred_at=datetime.now(UTC),
        content_fingerprint="revision-1",
    )

    builder.build(request, [])
    builder.build(request, [])

    episode_uuid = str(
        uuid5(
            UUID("8663df4a-c54a-42cc-9d4d-f4106379678a"),
            f"{request.memory_id}:{request.content_fingerprint}",
        )
    )
    group_id = f"org_{request.organization_id}_workspace_{request.workspace_id}"

    async def verify() -> None:
        from graphiti_core.nodes import EpisodicNode

        client = builder._client()
        try:
            _, driver, _ = client._resolve_request_scope(group_id)
            stored = await EpisodicNode.get_by_uuid(driver, episode_uuid)
            episodes = await client.retrieve_episodes(
                datetime.now(UTC), group_ids=[group_id], driver=driver
            )
            assert stored.uuid == episode_uuid
            assert [episode.uuid for episode in episodes].count(episode_uuid) == 1
        finally:
            await builder._settle_background_tasks(client)
            await client.close()

    asyncio.run(verify())

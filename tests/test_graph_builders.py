from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.helpers import validate_group_id

import albert.graph_builders as graph_builders
from albert.config import Settings
from albert.graph_builders import (
    _DEFAULT_CONFIDENCE,
    BuildRequest,
    GraphitiGraphBuilder,
    _canonicalize_graphiti_edge_entities,
    probe_graph_builder,
    set_graph_builder,
)
from albert.graph_cypher import FalkorDBGraphStore
from albert.graph_store import set_graph_store

from .conftest import create_identity, drain_jobs

FALKORDB_URL = os.environ.get("ALBERT_TEST_FALKORDB_URL")
GRAPHITI_API_KEY = os.environ.get("ALBERT_TEST_GRAPHITI_API_KEY")
GRAPHITI_BASE_URL = os.environ.get("ALBERT_TEST_GRAPHITI_BASE_URL")
GRAPHITI_CHAT_MODEL = os.environ.get("ALBERT_TEST_GRAPHITI_CHAT_MODEL")
GRAPHITI_EMBEDDING_MODEL = os.environ.get("ALBERT_TEST_GRAPHITI_EMBEDDING_MODEL")


def test_graphiti_canonicalizes_safe_entity_aliases_before_edge_validation() -> None:
    response = {
        "edges": [
            {
                "source_entity_name": "Albert system",
                "target_entity_name": "falkordb",
                "relation_type": "USES",
            },
            {
                "source_entity_name": "London",
                "target_entity_name": "unknown place",
                "relation_type": "PREFERS",
            },
        ]
    }

    result = _canonicalize_graphiti_edge_entities(
        response, ["Albert", "FalkorDB", "London office", "London city"]
    )

    assert result["edges"][0]["source_entity_name"] == "Albert"
    assert result["edges"][0]["target_entity_name"] == "FalkorDB"
    assert result["edges"][1]["source_entity_name"] == "London"
    assert result["edges"][1]["target_entity_name"] == "unknown place"


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


def test_graphiti_readiness_fails_closed_when_embedding_model_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadyGraph:
        def __init__(self) -> None:
            self.closed = False

        async def build_indices_and_constraints(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    graph = ReadyGraph()
    builder = GraphitiGraphBuilder(SimpleNamespace(embedding_dimensions=384))  # type: ignore[arg-type]
    builder._client = lambda: graph  # type: ignore[method-assign]
    monkeypatch.setattr(graph_builders, "classify", lambda _text: SimpleNamespace())

    class UnreachableEmbedder:
        def embed(self, _text: str) -> list[float]:
            raise httpx.ConnectError("secret-provider-url.example is unreachable")

    monkeypatch.setattr(graph_builders, "get_embedder", lambda: UnreachableEmbedder())
    set_graph_builder(builder)
    try:
        result = probe_graph_builder()
    finally:
        set_graph_builder(None)

    assert graph.closed is True
    assert result == {
        "ready": False,
        "name": "graphiti",
        "version": "0.30.2",
        "error": "ConnectError",
    }
    assert "secret-provider-url" not in str(result)


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
    assert "Every relationship endpoint" in fake.calls[0]["custom_extraction_instructions"]
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


class ScriptedGraphiti(FakeGraphiti):
    """A fake whose extraction result is built from the episode uuid in play."""

    def __init__(self, edges_for) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self._edges_for = edges_for
        self.nodes = [
            SimpleNamespace(uuid="source", name="Albert", labels=["Entity", "Product"]),
            SimpleNamespace(uuid="target", name="FalkorDB", labels=["Entity", "Database"]),
        ]

    async def add_episode(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        episode = self.episodes[kwargs["uuid"]]
        self.episode_contents.append(episode.content)  # type: ignore[attr-defined]
        episode.content = ""  # type: ignore[attr-defined]
        return SimpleNamespace(nodes=self.nodes, edges=self._edges_for(kwargs["uuid"]))


def _request(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "memory_id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": uuid4(),
        "subject": "Runtime",
        "content": "Albert runs on FalkorDB.",
        "occurred_at": datetime(2025, 12, 1, tzinfo=UTC),
        "content_fingerprint": "revision-1",
    }
    return BuildRequest(**{**base, **overrides})


def test_graphiti_only_attributes_edges_this_episode_produced() -> None:
    def edges_for(episode_uuid: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                uuid="owned",
                source_node_uuid="source",
                target_node_uuid="target",
                name="RUNS ON",
                episodes=[episode_uuid],
                attributes={},
                valid_at=datetime(2026, 1, 1, tzinfo=UTC),
                invalid_at=None,
                reference_time=None,
            ),
            # Superseded by this extraction but extracted from, and owned by, a
            # different memory. Its endpoints are deliberately absent from the
            # node set, as Graphiti leaves them when only the edge changed.
            SimpleNamespace(
                uuid="superseded-elsewhere",
                source_node_uuid="absent-source",
                target_node_uuid="absent-target",
                name="RUNS ON",
                episodes=["another-episode"],
                attributes={},
                valid_at=datetime(2025, 1, 1, tzinfo=UTC),
                invalid_at=datetime(2026, 1, 1, tzinfo=UTC),
                reference_time=None,
            ),
            # Still-open edge from another memory: not ours to write, and
            # nothing to close.
            SimpleNamespace(
                uuid="open-elsewhere",
                source_node_uuid="source",
                target_node_uuid="target",
                name="DEPLOYED WITH",
                episodes=["another-episode"],
                attributes={},
                valid_at=datetime(2025, 6, 1, tzinfo=UTC),
                invalid_at=None,
                reference_time=None,
            ),
        ]

    fake = ScriptedGraphiti(edges_for)
    builder = TestGraphitiBuilder(SimpleNamespace())  # type: ignore[arg-type]
    builder._client = lambda: fake  # type: ignore[method-assign]

    built = builder.build(_request(), [])

    by_id = {record.metadata["graphiti_edge_id"]: record for record in built}
    assert set(by_id) == {"owned", "superseded-elsewhere"}

    owned = by_id["owned"]
    assert owned.close_only is False
    assert owned.relationship is not None
    assert owned.relationship.relation_type == "runs_on"

    superseded = by_id["superseded-elsewhere"]
    assert superseded.close_only is True
    # Nothing about this edge may be attributed to the memory being enriched.
    assert superseded.relationship is None
    assert superseded.valid_until == datetime(2026, 1, 1, tzinfo=UTC)


def test_graphiti_records_whether_confidence_is_a_real_score() -> None:
    def edges_for(episode_uuid: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                uuid="scored",
                source_node_uuid="source",
                target_node_uuid="target",
                name="RUNS ON",
                episodes=[episode_uuid],
                attributes={"confidence": 0.91},
                valid_at=None,
                invalid_at=None,
                reference_time=None,
            ),
            SimpleNamespace(
                uuid="unscored",
                source_node_uuid="source",
                target_node_uuid="target",
                name="DEPLOYED WITH",
                episodes=[episode_uuid],
                attributes=None,
                valid_at=None,
                invalid_at=None,
                reference_time=None,
            ),
        ]

    fake = ScriptedGraphiti(edges_for)
    builder = TestGraphitiBuilder(SimpleNamespace())  # type: ignore[arg-type]
    builder._client = lambda: fake  # type: ignore[method-assign]

    built = {
        record.metadata["graphiti_edge_id"]: record for record in builder.build(_request(), [])
    }

    assert built["scored"].relationship.confidence == pytest.approx(0.91)
    assert built["scored"].metadata["confidence_source"] == "graphiti"
    # Graphiti does not score edges, so Albert's ranking cannot tell these apart.
    assert built["unscored"].relationship.confidence == pytest.approx(_DEFAULT_CONFIDENCE)
    assert built["unscored"].metadata["confidence_source"] == "builder-default"


def test_graphiti_driver_cleanup_failure_does_not_mask_the_build_failure() -> None:
    class FailingGraphiti(FakeGraphiti):
        async def add_episode(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("extraction failed")

    fake = FailingGraphiti()

    async def fail_initialization() -> None:
        raise RuntimeError("scoped index initialization failed")

    builder = TestGraphitiBuilder(SimpleNamespace())  # type: ignore[arg-type]
    builder._client = lambda: fake  # type: ignore[method-assign]

    async def verify() -> None:
        fake.driver._albert_scoped_drivers = {
            "scope": SimpleNamespace(_init_task=asyncio.create_task(fail_initialization()))
        }
        with pytest.raises(RuntimeError, match="extraction failed"):
            await builder._build(_request())
        assert fake.closed is True

    asyncio.run(verify())


def test_graphiti_core_private_api_surface_is_still_present() -> None:
    """The adapter reaches into graphiti-core internals, so the pin is load-bearing.

    If this fails after a version bump, re-verify ``GraphitiGraphBuilder``
    against the new release before relaxing it.
    """
    from importlib.metadata import version
    from inspect import getsource, signature

    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.nodes import EpisodicNode

    assert version("graphiti-core") == "0.30.2"
    for attribute in ("clone", "_get_graph", "build_indices_and_constraints", "default_group_id"):
        assert hasattr(FalkorDriver, attribute), attribute
    initializer = getsource(FalkorDriver.__init__)
    for assignment in ("self._database", "self.client", "self._init_task"):
        assert assignment in initializer, assignment
    assert hasattr(EpisodicNode, "get_by_uuid") and hasattr(EpisodicNode, "save")
    # _prepare_episode unpacks this into (group_id, driver, clients).
    assert len(signature(Graphiti._resolve_request_scope).return_annotation.__args__) == 3


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
            first_previous = await builder._prepare_episode(client, request, episode_uuid, group_id)
            _, driver, _ = client._resolve_request_scope(group_id)
            stored = await EpisodicNode.get_by_uuid(driver, episode_uuid)
            retry_previous = await builder._prepare_episode(client, request, episode_uuid, group_id)
            episodes = await client.retrieve_episodes(
                datetime.now(UTC),
                group_ids=[group_id],
                source=builder._episode_type(),
                driver=driver,
            )
            assert stored.uuid == episode_uuid
            assert [episode.uuid for episode in episodes] == [episode_uuid]
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

    first = builder.build(request, [])
    retry = builder.build(request, [])

    for relationships in (first, retry):
        uses = [
            item.relationship
            for item in relationships
            if item.relationship is not None and item.relationship.relation_type == "uses"
        ]
        assert len(uses) == 1
        assert uses[0].source.name.casefold() == "albert"
        assert uses[0].target.name.casefold() == "falkordb"

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


@pytest.mark.anyio
async def test_graphiti_live_worker_api_relationship_journey() -> None:
    """Exercise Graphiti through the customer-visible worker and graph API."""
    required = (
        FALKORDB_URL,
        GRAPHITI_API_KEY,
        GRAPHITI_BASE_URL,
        GRAPHITI_CHAT_MODEL,
        GRAPHITI_EMBEDDING_MODEL,
    )
    if not all(required):
        pytest.skip("live FalkorDB and Graphiti model test settings are not set")

    from albert.api import app

    graph_name = f"graphiti_live_api_{uuid4().hex}"
    settings = Settings(
        graph_builder="graphiti",
        graph_store="falkordb",
        falkordb_url=FALKORDB_URL,
        falkordb_graph=graph_name,
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
    set_graph_builder(GraphitiGraphBuilder(settings))
    set_graph_store(FalkorDBGraphStore(FALKORDB_URL, graph_name=graph_name))

    first_key, _first_org, _first_workspace = create_identity("Graphiti live first")
    second_key, _second_org, _second_workspace = create_identity("Graphiti live second")

    def matching(rows: list[dict], relation: str, source: str, target: str) -> list[dict]:
        return [
            row
            for row in rows
            if row["relation_type"] == relation
            and row["source_name"].casefold() == source.casefold()
            and row["target_name"].casefold() == target.casefold()
        ]

    async def query(client: httpx.AsyncClient, text: str, **extra: str) -> list[dict]:
        response = await client.post("/v1/graph/query", json={"query": text, **extra})
        assert response.status_code == 200, response.text
        return response.json()

    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {first_key}"},
            ) as first,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {second_key}"},
            ) as second,
        ):
            first_memories: dict[str, str] = {}
            second_memories: dict[str, str] = {}
            fixtures = {
                "uses": "Albert uses FalkorDB for temporal graph memory.",
                "prefers": "Avery prefers London for the annual planning retreat.",
            }
            for relation, content in fixtures.items():
                for client, memories in (
                    (first, first_memories),
                    (second, second_memories),
                ):
                    response = await client.post("/v1/memories", json={"content": content})
                    assert response.status_code == 201, response.text
                    memories[relation] = response.json()["id"]
            drain_jobs()

            first_uses = matching(
                await query(first, "Albert FalkorDB"), "uses", "Albert", "FalkorDB"
            )
            first_prefers = matching(
                await query(first, "Avery London"), "prefers", "Avery", "London"
            )
            second_uses = matching(
                await query(second, "Albert FalkorDB"), "uses", "Albert", "FalkorDB"
            )
            second_prefers = matching(
                await query(second, "Avery London"), "prefers", "Avery", "London"
            )
            assert [row["source_memory_id"] for row in first_uses] == [first_memories["uses"]]
            assert [row["source_memory_id"] for row in first_prefers] == [first_memories["prefers"]]
            assert [row["source_memory_id"] for row in second_uses] == [second_memories["uses"]]
            assert [row["source_memory_id"] for row in second_prefers] == [
                second_memories["prefers"]
            ]

            retried = await first.patch(
                f"/v1/memories/{first_memories['uses']}",
                json={"content": fixtures["uses"]},
            )
            assert retried.status_code == 200, retried.text
            drain_jobs()
            retried_uses = matching(
                await query(first, "Albert FalkorDB"), "uses", "Albert", "FalkorDB"
            )
            assert len(retried_uses) == 1
            before_change = datetime.now(UTC)

            changed = await first.patch(
                f"/v1/memories/{first_memories['uses']}",
                json={"content": "Albert uses Neo4j for temporal graph memory."},
            )
            assert changed.status_code == 200, changed.text
            drain_jobs()
            assert (
                matching(await query(first, "Albert FalkorDB"), "uses", "Albert", "FalkorDB") == []
            )
            assert len(matching(await query(first, "Albert Neo4j"), "uses", "Albert", "Neo4j")) == 1
            historical = await query(
                first, "Albert FalkorDB", temporal_as_of=before_change.isoformat()
            )
            assert len(matching(historical, "uses", "Albert", "FalkorDB")) == 1

            assert {row["source_memory_id"] for row in await query(first, "Avery London")} == {
                first_memories["prefers"]
            }
            assert {row["source_memory_id"] for row in await query(second, "Avery London")} == {
                second_memories["prefers"]
            }
    finally:
        set_graph_builder(None)
        set_graph_store(None)

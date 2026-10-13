"""Graph extraction adapters.

The default builders keep Albert's existing classifier path.  Graphiti owns its
temporal extraction graph in an isolated FalkorDB graph (or the configured
Neo4j database) and the adapter copies its resolved facts into Albert's
authorization-aware ``GraphStore`` contract.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version
from typing import Any, Protocol
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid5

from albert.classifier import ExtractedEntity, ExtractedRelationship
from albert.config import Settings, get_settings

_EPISODE_NAMESPACE = UUID("8663df4a-c54a-42cc-9d4d-f4106379678a")


@dataclass(frozen=True)
class BuildRequest:
    memory_id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    subject: str
    content: str
    occurred_at: datetime
    content_fingerprint: str


@dataclass(frozen=True)
class BuiltRelationship:
    relationship: ExtractedRelationship
    valid_from: datetime
    valid_until: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    close_only: bool = False


class GraphBuilder(Protocol):
    name: str
    version: str

    def build(
        self, request: BuildRequest, fallback: list[ExtractedRelationship]
    ) -> list[BuiltRelationship]: ...

    def readiness(self) -> str: ...


class ClassifierGraphBuilder:
    version = "built-in"

    def __init__(self, name: str) -> None:
        self.name = name

    def build(
        self, request: BuildRequest, fallback: list[ExtractedRelationship]
    ) -> list[BuiltRelationship]:
        return [
            BuiltRelationship(relationship=edge, valid_from=request.occurred_at)
            for edge in fallback
        ]

    def readiness(self) -> str:
        return "built-in"


def _relation_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return normalized[:100] or "related_to"


def _entity_type(node: Any) -> str:
    labels = [str(label).casefold() for label in getattr(node, "labels", [])]
    return next((label for label in labels if label != "entity"), "concept")[:100]


def _graphiti_group_id(organization_id: UUID, workspace_id: UUID | None) -> str:
    """Encode Albert's scope using Graphiti's restricted group-id alphabet."""
    workspace = str(workspace_id) if workspace_id is not None else "global"
    return f"org_{organization_id}_workspace_{workspace}"


class GraphitiGraphBuilder:
    name = "graphiti"

    @property
    def version(self) -> str:
        return version("graphiti-core")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client(self) -> Any:
        # Imports stay lazy so default installations do not initialize Graphiti.
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.driver.neo4j_driver import Neo4jDriver
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        assert self.settings.openai_api_key is not None
        assert self.settings.classification_model is not None
        secret = self.settings.openai_api_key.get_secret_value()
        model_config = LLMConfig(
            api_key=secret,
            model=self.settings.classification_model,
            small_model=self.settings.classification_model,
            base_url=self.settings.openai_base_url,
            temperature=0,
        )
        if self.settings.graph_store == "falkordb":

            class CompatibleFalkorDriver(FalkorDriver):
                """Use the FalkorDB client DDL supported by Albert's pinned image.

                Graphiti 0.30.2 emits an obsolete multi-argument full-text
                procedure call. The official client produces equivalent DDL
                accepted by FalkorDB 6.0.1 and keeps index creation idempotent.
                """

                def clone(self, database: str) -> Any:
                    """Keep Albert's compatible DDL on request-scoped drivers.

                    Graphiti's Falkor driver constructs the base class here,
                    which loses this subclass's index implementation. Cache the
                    scoped driver as well so episode preparation and
                    ``add_episode`` share one initialized request scope.
                    """
                    if database == self._database:
                        return self
                    clones = getattr(self, "_albert_scoped_drivers", None)
                    if clones is None:
                        clones = {}
                        self._albert_scoped_drivers = clones
                    if database not in clones:
                        clone_database = (
                            "default_db" if database == self.default_group_id else database
                        )
                        clones[database] = type(self)(
                            falkor_db=self.client,
                            database=clone_database,
                        )
                    return clones[database]

                async def build_indices_and_constraints(
                    self, delete_existing: bool = False
                ) -> None:
                    if delete_existing:
                        raise ValueError("Graphiti index deletion is not supported by Albert")
                    graph = self._get_graph(self._database)
                    # FalkorDB 6.0.1 returns no rows for exact group_id
                    # predicates when that property has a range index. Each
                    # request already uses a group-isolated graph, so leave
                    # group_id unindexed and preserve correct Graphiti reads.
                    definitions = (
                        (
                            graph.create_node_fulltext_index,
                            "Episodic",
                            ("content", "source", "source_description", "group_id"),
                        ),
                        (
                            graph.create_node_fulltext_index,
                            "Entity",
                            ("name", "summary", "group_id"),
                        ),
                        (graph.create_node_fulltext_index, "Community", ("name", "group_id")),
                        (
                            graph.create_edge_fulltext_index,
                            "RELATES_TO",
                            ("name", "fact", "group_id"),
                        ),
                        (
                            graph.create_node_range_index,
                            "Entity",
                            ("uuid",),
                        ),
                        (graph.create_node_range_index, "Entity", ("name",)),
                        (graph.create_node_range_index, "Entity", ("created_at",)),
                        (
                            graph.create_node_range_index,
                            "Episodic",
                            ("uuid",),
                        ),
                        (graph.create_node_range_index, "Episodic", ("created_at",)),
                        (graph.create_node_range_index, "Episodic", ("valid_at",)),
                        (graph.create_node_range_index, "Community", ("uuid",)),
                        (graph.create_node_range_index, "Saga", ("uuid",)),
                        (graph.create_node_range_index, "Saga", ("name",)),
                        (
                            graph.create_edge_range_index,
                            "RELATES_TO",
                            ("uuid",),
                        ),
                        (graph.create_edge_range_index, "RELATES_TO", ("name",)),
                        (graph.create_edge_range_index, "RELATES_TO", ("created_at",)),
                        (graph.create_edge_range_index, "RELATES_TO", ("expired_at",)),
                        (graph.create_edge_range_index, "RELATES_TO", ("valid_at",)),
                        (graph.create_edge_range_index, "RELATES_TO", ("invalid_at",)),
                        (graph.create_edge_range_index, "MENTIONS", ("uuid",)),
                        (graph.create_edge_range_index, "HAS_MEMBER", ("uuid",)),
                        (graph.create_edge_range_index, "HAS_EPISODE", ("uuid",)),
                        (graph.create_edge_range_index, "NEXT_EPISODE", ("uuid",)),
                    )
                    for create, label, properties in definitions:
                        try:
                            await create(label, *properties)
                        except Exception as exc:
                            message = str(exc).casefold()
                            if "already indexed" not in message and "already exists" not in message:
                                raise

            parsed = urlparse(self.settings.falkordb_url)
            driver = CompatibleFalkorDriver(
                host=parsed.hostname or "localhost",
                port=parsed.port or 6379,
                username=parsed.username or None,
                password=unquote(parsed.password) if parsed.password else None,
                database=f"{self.settings.falkordb_graph}_graphiti",
            )
        else:
            assert self.settings.neo4j_password is not None
            driver = Neo4jDriver(
                self.settings.neo4j_url,
                self.settings.neo4j_user,
                self.settings.neo4j_password.get_secret_value(),
                database=self.settings.neo4j_database,
            )
        llm = OpenAIGenericClient(config=model_config)
        embedder = OpenAIEmbedder(
            OpenAIEmbedderConfig(
                api_key=secret,
                base_url=self.settings.openai_base_url,
                embedding_model=self.settings.embedding_model,
                embedding_dim=self.settings.embedding_dimensions,
            )
        )
        return Graphiti(
            graph_driver=driver,
            llm_client=llm,
            embedder=embedder,
            cross_encoder=OpenAIRerankerClient(config=model_config),
            store_raw_episode_content=False,
        )

    def _episode_type(self) -> Any:
        from graphiti_core.nodes import EpisodeType

        return EpisodeType.text

    @staticmethod
    async def _get_episode(driver: Any, episode_uuid: str) -> Any:
        from graphiti_core.nodes import EpisodicNode

        return await EpisodicNode.get_by_uuid(driver, episode_uuid)

    @staticmethod
    async def _save_episode(driver: Any, episode: Any) -> None:
        await episode.save(driver)

    async def _prepare_episode(
        self,
        client: Any,
        request: BuildRequest,
        episode_uuid: str,
        group_id: str,
    ) -> list[str]:
        """Upsert Graphiti's deterministic episode and return prior context IDs.

        Graphiti 0.30.2 interprets a supplied ``add_episode`` UUID as an
        existing-node lookup.  Saving the episode first preserves stable retry
        identity while using that API as intended.  The new node is excluded
        from its own temporal context.
        """
        from graphiti_core.errors import NodeNotFoundError
        from graphiti_core.nodes import EpisodicNode

        init_task = getattr(client.driver, "_init_task", None)
        if init_task is not None:
            await init_task

        _, driver, _ = client._resolve_request_scope(group_id)
        scoped_init_task = getattr(driver, "_init_task", None)
        if scoped_init_task is not None:
            await scoped_init_task
        previous = await client.retrieve_episodes(
            request.occurred_at,
            group_ids=[group_id],
            source=self._episode_type(),
            driver=driver,
        )
        previous_ids = [episode.uuid for episode in previous if episode.uuid != episode_uuid]
        try:
            episode = await self._get_episode(driver, episode_uuid)
        except NodeNotFoundError:
            episode = EpisodicNode(
                uuid=episode_uuid,
                name=request.subject or f"memory-{request.memory_id}",
                group_id=group_id,
                labels=[],
                source=self._episode_type(),
                content=request.content,
                source_description="Albert memory",
                valid_at=request.occurred_at,
            )
        else:
            # Successful processing clears raw content. Restore this revision's
            # body before a retry so Graphiti reprocesses the same input.
            episode.name = request.subject or f"memory-{request.memory_id}"
            episode.group_id = group_id
            episode.source = self._episode_type()
            episode.content = request.content
            episode.source_description = "Albert memory"
            episode.valid_at = request.occurred_at
        await self._save_episode(driver, episode)
        return previous_ids

    @staticmethod
    async def _settle_background_tasks(client: Any) -> None:
        """Await and retrieve request-scoped driver initialization results."""
        drivers = getattr(client.driver, "_albert_scoped_drivers", {}).values()
        tasks = [
            task
            for driver in drivers
            if (task := getattr(driver, "_init_task", None)) is not None
        ]
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _build(self, request: BuildRequest) -> list[BuiltRelationship]:
        client = self._client()
        try:
            episode_uuid = str(
                uuid5(
                    _EPISODE_NAMESPACE,
                    f"{request.memory_id}:{request.content_fingerprint}",
                )
            )
            group_id = _graphiti_group_id(request.organization_id, request.workspace_id)
            previous_episode_uuids = await self._prepare_episode(
                client, request, episode_uuid, group_id
            )
            result = await client.add_episode(
                name=request.subject or f"memory-{request.memory_id}",
                episode_body=request.content,
                source_description="Albert memory",
                reference_time=request.occurred_at,
                source=self._episode_type(),
                group_id=group_id,
                uuid=episode_uuid,
                previous_episode_uuids=previous_episode_uuids,
                custom_extraction_instructions=(
                    "Extract only facts supported by this memory. Do not extract credentials or "
                    "secrets. Preserve changes and contradictions as temporal facts."
                ),
            )
            nodes = {node.uuid: node for node in result.nodes}
            built: list[BuiltRelationship] = []
            for edge in result.edges:
                source = nodes.get(edge.source_node_uuid)
                target = nodes.get(edge.target_node_uuid)
                if source is None or target is None:
                    continue
                built.append(
                    BuiltRelationship(
                        relationship=ExtractedRelationship(
                            source=ExtractedEntity(source.name, _entity_type(source)),
                            relation_type=_relation_name(edge.name),
                            target=ExtractedEntity(target.name, _entity_type(target)),
                            confidence=float((edge.attributes or {}).get("confidence", 0.7)),
                        ),
                        valid_from=edge.valid_at or edge.reference_time or request.occurred_at,
                        valid_until=edge.invalid_at,
                        metadata={
                            "extracted": True,
                            "builder": "graphiti",
                            "graphiti_edge_id": edge.uuid,
                            "graphiti_episode_id": episode_uuid,
                        },
                        close_only=bool(
                            edge.invalid_at is not None and episode_uuid not in edge.episodes
                        ),
                    )
                )
            return built
        finally:
            try:
                await self._settle_background_tasks(client)
            finally:
                await client.close()

    def build(
        self, request: BuildRequest, fallback: list[ExtractedRelationship]
    ) -> list[BuiltRelationship]:
        del fallback
        return asyncio.run(self._build(request))

    def readiness(self) -> str:
        async def probe() -> None:
            client = self._client()
            try:
                await client.build_indices_and_constraints()
            finally:
                await client.close()

        asyncio.run(probe())
        return self.version


_override: GraphBuilder | None = None
_instances: dict[str, GraphBuilder] = {}


def set_graph_builder(builder: GraphBuilder | None) -> None:
    global _override
    _override = builder


def get_graph_builder() -> GraphBuilder:
    if _override is not None:
        return _override
    settings = get_settings()
    name = settings.graph_builder or "regex"
    if name not in _instances:
        _instances[name] = (
            GraphitiGraphBuilder(settings) if name == "graphiti" else ClassifierGraphBuilder(name)
        )
    return _instances[name]

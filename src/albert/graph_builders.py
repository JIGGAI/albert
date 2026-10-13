"""Graph extraction adapters.

The default builders keep Albert's existing classifier path.  Graphiti owns its
temporal extraction graph in an isolated FalkorDB graph (or the configured
Neo4j database) and the adapter copies its resolved facts into Albert's
authorization-aware ``GraphStore`` contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version
from typing import Any, Protocol
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid5

from albert.classifier import ExtractedEntity, ExtractedRelationship, classify
from albert.config import Settings, get_settings
from albert.embeddings import get_embedder

logger = logging.getLogger("albert.graph_builders")

_EPISODE_NAMESPACE = UUID("8663df4a-c54a-42cc-9d4d-f4106379678a")

# Graphiti does not score the edges it extracts.  Albert ranks relationships by
# confidence (see ``graph.relationships_for_memory``) and returns it as the
# recall score, so every unscored Graphiti edge lands on this one value and
# ranking among them is effectively insertion order.  Each edge records whether
# its confidence is a real score or this placeholder under the
# ``confidence_source`` metadata key.
_DEFAULT_CONFIDENCE = 0.7

_GRAPHITI_EXTRACTION_INSTRUCTIONS = (
    "Extract only facts supported by this memory. Do not extract credentials or secrets. "
    "Preserve changes and contradictions as temporal facts. Every relationship endpoint must "
    "also be extracted as an entity. In the relationship response, copy each source and target "
    "entity name exactly from the supplied entity list; do not add qualifiers or aliases."
)


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
    """One edge a builder resolved for a memory.

    ``close_only`` records carry no ``relationship``: they name an edge that
    some other memory owns and that this extraction superseded, so there is
    nothing to write, only a validity window to end.
    """

    valid_from: datetime
    relationship: ExtractedRelationship | None = None
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


def _normalized_entity_name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _canonical_entity_name(value: str, entity_names: list[str]) -> str:
    """Map harmless model aliases back to Graphiti's extracted entity spelling.

    Graphiti 0.30.2 extracts nodes and edges in separate model calls, then drops
    an edge unless both endpoint strings exactly match the node response. Some
    OpenAI-compatible models add a generic qualifier (for example, ``database``)
    in only one response. Resolve only exact normalized or unique containment
    matches; ambiguous endpoints remain unchanged and Graphiti fails them closed.
    """
    normalized = _normalized_entity_name(value)
    exact = [name for name in entity_names if _normalized_entity_name(name) == normalized]
    if len(exact) == 1:
        return exact[0]

    contained = [
        name
        for name in entity_names
        if normalized
        and (candidate := _normalized_entity_name(name))
        and (f" {candidate} " in f" {normalized} " or f" {normalized} " in f" {candidate} ")
    ]
    return contained[0] if len(contained) == 1 else value


def _canonicalize_graphiti_edge_entities(
    response: dict[str, Any], entity_names: list[str]
) -> dict[str, Any]:
    for edge in response.get("edges", []):
        if not isinstance(edge, dict):
            continue
        for field_name in ("source_entity_name", "target_entity_name"):
            value = edge.get(field_name)
            if isinstance(value, str):
                edge[field_name] = _canonical_entity_name(value, entity_names)
    return response


def _graphiti_prompt_entity_names(messages: list[Any]) -> list[str]:
    """Read the exact resolved entity names supplied to edge extraction."""
    for message in reversed(messages):
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            continue
        match = re.search(r"<ENTITIES>\s*(.*?)\s*</ENTITIES>", content, re.DOTALL)
        if match is None:
            continue
        try:
            entities = json.loads(match.group(1))
        except (TypeError, ValueError):
            return []
        return [
            item["name"]
            for item in entities
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
    return []


class GraphitiGraphBuilder:
    name = "graphiti"

    @property
    def version(self) -> str:
        return version("graphiti-core")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client(self) -> Any:
        # graphiti-core is a hard dependency, but importing it pulls in its own
        # model and driver stack.  Keep the imports lazy so installations that
        # never select this builder do not pay for them.
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.driver.neo4j_driver import Neo4jDriver
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        class EntityConsistentOpenAIClient(OpenAIGenericClient):
            """Keep Graphiti's separate node/edge extraction calls consistent."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._albert_entity_names: list[str] = []
                self._albert_force_entity_names: set[str] = set()
                self._albert_missing_entity_names: set[str] = set()

            async def generate_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                response = await super().generate_response(*args, **kwargs)
                response_model = kwargs.get("response_model")
                if response_model is None and len(args) > 1:
                    response_model = args[1]
                model_name = getattr(response_model, "__name__", "")
                if model_name == "ExtractedEntities":
                    extracted = response.get("extracted_entities", [])
                    present = {
                        item["name"]
                        for item in extracted
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    }
                    extracted.extend(
                        {
                            "name": name,
                            "entity_type_id": 0,
                            "episode_indices": [0],
                        }
                        for name in sorted(self._albert_force_entity_names - present)
                    )
                    self._albert_force_entity_names.clear()
                    self._albert_entity_names = [
                        item["name"]
                        for item in extracted
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    ]
                elif model_name == "ExtractedEdges":
                    messages = kwargs.get("messages") or (args[0] if args else [])
                    entity_names = _graphiti_prompt_entity_names(messages)
                    response = _canonicalize_graphiti_edge_entities(
                        response, entity_names or self._albert_entity_names
                    )
                    available = set(entity_names or self._albert_entity_names)
                    self._albert_missing_entity_names = {
                        value
                        for edge in response.get("edges", [])
                        if isinstance(edge, dict)
                        for field_name in ("source_entity_name", "target_entity_name")
                        if isinstance((value := edge.get(field_name)), str)
                        and value not in available
                    }
                return response

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
        llm = EntityConsistentOpenAIClient(config=model_config)
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
            task for driver in drivers if (task := getattr(driver, "_init_task", None)) is not None
        ]
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _build(self, request: BuildRequest) -> list[BuiltRelationship]:
        client = self._client()
        failing = False
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
            for attempt in range(2):
                result = await client.add_episode(
                    name=request.subject or f"memory-{request.memory_id}",
                    episode_body=request.content,
                    source_description="Albert memory",
                    reference_time=request.occurred_at,
                    source=self._episode_type(),
                    group_id=group_id,
                    uuid=episode_uuid,
                    previous_episode_uuids=previous_episode_uuids,
                    custom_extraction_instructions=_GRAPHITI_EXTRACTION_INSTRUCTIONS,
                )
                llm_client = getattr(client, "llm_client", None)
                missing = set(getattr(llm_client, "_albert_missing_entity_names", ()))
                if not missing:
                    break
                if attempt == 1:
                    raise ValueError("Graphiti relationship endpoints could not be resolved")
                llm_client._albert_force_entity_names = missing
                llm_client._albert_missing_entity_names.clear()
                previous_episode_uuids = await self._prepare_episode(
                    client, request, episode_uuid, group_id
                )
            nodes = {node.uuid: node for node in result.nodes}
            built: list[BuiltRelationship] = []
            for edge in result.edges:
                provenance = {
                    "extracted": True,
                    "builder": "graphiti",
                    "graphiti_edge_id": edge.uuid,
                    "graphiti_episode_id": episode_uuid,
                }
                if episode_uuid not in (edge.episodes or ()):
                    # Graphiti also returns edges it resolved or invalidated
                    # from earlier episodes.  Writing one here would hand this
                    # memory's owner, workspace and sensitivity to a fact
                    # extracted from different source material, so record the
                    # invalidation and leave the edge to the memory that owns
                    # it.  This runs before node resolution on purpose: a
                    # superseded edge's endpoints are often absent from this
                    # episode's node set.
                    if edge.invalid_at is not None:
                        built.append(
                            BuiltRelationship(
                                valid_from=edge.valid_at or request.occurred_at,
                                valid_until=edge.invalid_at,
                                metadata=provenance,
                                close_only=True,
                            )
                        )
                    continue
                source = nodes.get(edge.source_node_uuid)
                target = nodes.get(edge.target_node_uuid)
                if source is None or target is None:
                    continue
                attributes = edge.attributes or {}
                built.append(
                    BuiltRelationship(
                        valid_from=edge.valid_at or edge.reference_time or request.occurred_at,
                        relationship=ExtractedRelationship(
                            source=ExtractedEntity(source.name, _entity_type(source)),
                            relation_type=_relation_name(edge.name),
                            target=ExtractedEntity(target.name, _entity_type(target)),
                            confidence=float(attributes.get("confidence", _DEFAULT_CONFIDENCE)),
                        ),
                        valid_until=edge.invalid_at,
                        metadata={
                            **provenance,
                            "confidence_source": (
                                "graphiti" if "confidence" in attributes else "builder-default"
                            ),
                        },
                    )
                )
            return built
        except BaseException:
            failing = True
            raise
        finally:
            try:
                await self._settle_background_tasks(client)
            except Exception:
                # An exception raised here would replace the build failure that
                # is already propagating, which is the one worth reporting.
                if not failing:
                    raise
                logger.warning("Scoped driver initialization also failed", exc_info=True)
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
        # Index creation proves only that the graph store is reachable. A
        # Graphiti indexing job also requires both OpenAI-compatible model
        # paths, so exercise Albert's normal abstractions before claiming the
        # builder is ready. The caller reports exception types only, keeping
        # provider URLs, responses, and credentials out of operator telemetry.
        classify("Albert runtime readiness probe.")
        vector = get_embedder().embed("Albert runtime readiness probe")
        if len(vector) != self.settings.embedding_dimensions:
            raise ValueError("Embedding provider returned unexpected dimensions")
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


def builder_status() -> dict[str, Any]:
    """Describe the configured builder without touching the network.

    Health endpoints use this: a model or graph outage must not take the API
    out of rotation, because reads and search keep working without the
    builder.  It still catches a builder that cannot be constructed at all --
    a missing ``graphiti-core``, for instance.
    """
    configured = get_settings().graph_builder or "regex"
    try:
        builder = get_graph_builder()
        return {"name": builder.name, "version": builder.version, "error": None}
    except Exception as exc:
        logger.warning("Graph builder is unavailable", exc_info=True)
        return {"name": configured, "version": None, "error": type(exc).__name__}


def probe_graph_builder() -> dict[str, Any]:
    """Execute the builder's own readiness check.

    Unlike :func:`builder_status` this reaches the model and graph backends,
    so it belongs behind an operator view or ``albert-admin validate-runtime``
    rather than in a liveness path.
    """
    status = builder_status()
    if status["error"] is not None:
        return {"ready": False, **status}
    try:
        version = get_graph_builder().readiness()
    except Exception as exc:
        logger.warning("Graph builder readiness probe failed", exc_info=True)
        return {"ready": False, **status, "error": type(exc).__name__}
    return {"ready": True, "name": status["name"], "version": version, "error": None}

"""The knowledge-graph storage interface and the catalogue of stores.

Every graph read and write in Albert goes through a `GraphStore`. PostgreSQL is
the built-in store; others plug in by implementing the same interface. The
types here are store-neutral on purpose: nothing outside a store's own module
may assume the graph lives in SQL tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from albert.classifier import ExtractedRelationship
from albert.schemas import RelationshipRead


@dataclass(frozen=True)
class GraphEntity:
    id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    name: str
    entity_type: str
    description: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class StoredRelationship:
    """One edge with both endpoints, as any store can return it."""

    id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    source: GraphEntity
    target: GraphEntity
    relation_type: str
    sensitivity: str
    source_memory_id: UUID | None
    confidence: float
    valid_from: datetime
    valid_until: datetime | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def read(self) -> RelationshipRead:
        return RelationshipRead(
            id=self.id,
            source_entity_id=self.source.id,
            source_name=self.source.name,
            source_type=self.source.entity_type,
            relation_type=self.relation_type,
            sensitivity=self.sensitivity,
            target_entity_id=self.target.id,
            target_name=self.target.name,
            target_type=self.target.entity_type,
            source_memory_id=self.source_memory_id,
            confidence=self.confidence,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
        )


class GraphStore(Protocol):
    """What Albert needs from a knowledge-graph backend.

    Every method receives the request's SQLAlchemy session. The PostgreSQL store
    writes through it, so graph changes commit or roll back with the memory they
    belong to. A store that keeps its data elsewhere cannot join that
    transaction: it must make each write idempotent (the worker retries failed
    jobs) and must enforce `organization_id`, `workspace_id`, sensitivity and
    validity filters itself.
    """

    name: str

    def add_relationship(
        self,
        session: Session,
        *,
        organization_id: UUID,
        workspace_id: UUID | None,
        extracted: ExtractedRelationship,
        source_memory_id: UUID | None,
        sensitivity: str,
        valid_from: datetime,
        valid_until: datetime | None = None,
        metadata: dict | None = None,
    ) -> tuple[StoredRelationship, bool]:
        """Create or reuse an open edge, upserting both entities; (edge, created)."""
        ...

    def close_memory_edges(
        self,
        session: Session,
        *,
        organization_id: UUID,
        memory_id: UUID,
        at: datetime,
        extracted_only: bool,
    ) -> int:
        """End the validity of a memory's still-open edges at `at`; returns how many."""
        ...

    def retarget_memory_edges(
        self,
        session: Session,
        *,
        organization_id: UUID,
        memory_id: UUID,
        sensitivity: str,
        valid_until: datetime | None,
    ) -> int:
        """Make a memory's extracted edges follow its new sensitivity and validity."""
        ...

    def query(
        self,
        session: Session,
        *,
        organization_id: UUID,
        workspace_id: UUID | None,
        query: str,
        temporal_as_of: datetime | None,
        limit: int,
        sensitivities: list[str],
    ) -> list[RelationshipRead]:
        """Edges whose entities or relation match the words of a query."""
        ...

    def get_entity(
        self, session: Session, *, entity_id: UUID, organization_id: UUID | None = None
    ) -> GraphEntity | None: ...

    def subgraph(
        self,
        session: Session,
        *,
        organization_id: UUID,
        workspace_id: UUID | None,
        entity_id: UUID,
        hops: int,
        temporal_as_of: datetime | None,
        limit: int,
        sensitivities: list[str],
    ) -> list[RelationshipRead]:
        """Edges within `hops` of an entity."""
        ...

    def export_relationships(
        self,
        session: Session,
        *,
        organization_id: UUID,
        workspace_id: UUID | None,
        sensitivities: list[str],
    ) -> list[StoredRelationship]:
        """Explicit (not extracted) edges for a portable export."""
        ...

    def snapshot(
        self,
        session: Session,
        *,
        organization_id: UUID,
        workspace_id: UUID | None,
        when: datetime,
        limit: int,
    ) -> tuple[list[GraphEntity], list[StoredRelationship], bool]:
        """Newest entities and the edges among them valid at `when`; (…, …, truncated)."""
        ...

    def counts(self, session: Session, *, when: datetime) -> dict[UUID, tuple[int, int]]:
        """Per organization: (entities, edges valid at `when`)."""
        ...

    def fingerprint(self, session: Session, *, organization_id: UUID) -> str:
        """Changes whenever the organization's graph does; used as a cache key."""
        ...


@dataclass(frozen=True)
class GraphStoreOption:
    summary: str
    choose_when: str
    available: bool


# The catalogue is the single source for docs, health output and the setting's
# error message. A store is `available` only when its adapter ships in this build.
GRAPH_STORES: dict[str, GraphStoreOption] = {
    "postgres": GraphStoreOption(
        summary="Entities and edges as tables in Albert's own PostgreSQL database.",
        choose_when=(
            "The default. One database to run and back up, graph writes commit with the "
            "memory they came from. Suits lookups of one or two hops."
        ),
        available=True,
    ),
    "falkordb": GraphStoreOption(
        summary="A Redis-based graph database queried with Cypher.",
        choose_when=(
            "Deep multi-hop traversal is a core workload and you want a small footprint "
            "on a single host. Also the lightest database Graphiti can build on. "
            "Graph writes no longer commit with the memory they came from."
        ),
        available=True,
    ),
    "neo4j": GraphStoreOption(
        summary="The established graph database: Cypher, graph algorithms, clustering.",
        choose_when=(
            "You need its ecosystem (graph data science, visual tooling, enterprise "
            "support) or already operate it. The heaviest option to run."
        ),
        available=False,
    ),
}

_override: GraphStore | None = None
_instances: dict[str, GraphStore] = {}


def set_graph_store(store: GraphStore | None) -> None:
    """Install a store for this process (tests, embedding); None restores the setting."""
    global _override
    _override = store


def get_graph_store() -> GraphStore:
    if _override is not None:
        return _override
    from albert.config import get_settings

    name = get_settings().graph_store
    if name not in _instances:
        if name == "postgres":
            from albert.graph import PostgresGraphStore

            _instances[name] = PostgresGraphStore()
        elif name == "falkordb":
            from albert.graph_falkordb import FalkorDBGraphStore

            settings = get_settings()
            _instances[name] = FalkorDBGraphStore(
                settings.falkordb_url, graph_name=settings.falkordb_graph
            )
        else:  # the setting's validator refuses anything not shipped
            raise RuntimeError(f"Graph store {name!r} is not available in this build")
    return _instances[name]

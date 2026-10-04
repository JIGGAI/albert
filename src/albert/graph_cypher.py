"""Graph stores that speak Cypher: FalkorDB and Neo4j.

Neither can join Albert's PostgreSQL transaction, so every write here is
idempotent (the worker retries failed jobs) and every read carries the
organization, workspace, sensitivity and validity filters itself. Times are
stored as epoch seconds and ids as strings; relation types are a property on a
single `REL` edge type, because Cypher cannot parameterize an edge type.

The queries are shared. A subclass only connects, creates indexes and runs a
query, returning rows whose nodes are plain property mappings.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from albert.classifier import ExtractedEntity, ExtractedRelationship
from albert.graph_store import GraphEntity, StoredRelationship
from albert.schemas import RelationshipRead

NO_WORKSPACE = ""
EDGE_FIELDS = (
    "r.id, r.workspace_id, r.relation_type, r.sensitivity, r.source_memory_id, "
    "r.confidence, r.valid_from, r.valid_until, r.metadata, s, t"
)
VALID_AT = "r.valid_from <= $when AND (r.valid_until IS NULL OR r.valid_until > $when)"


def _canonical(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _ws(workspace_id: UUID | None) -> str:
    return str(workspace_id) if workspace_id is not None else NO_WORKSPACE


def _epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).timestamp()


def _time(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def _uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


def _entity(data: Any) -> GraphEntity:
    return GraphEntity(
        id=UUID(data["id"]),
        organization_id=UUID(data["organization_id"]),
        workspace_id=_uuid(data.get("workspace_id")),
        name=data["name"],
        entity_type=data["entity_type"],
        description=data.get("description") or "",
        confidence=float(data.get("confidence", 1.0)),
    )


def _stored(row: list[Any]) -> StoredRelationship:
    (
        edge_id,
        workspace_id,
        relation_type,
        sensitivity,
        memory_id,
        confidence,
        valid_from,
        valid_until,
        metadata,
        source,
        target,
    ) = row
    source_entity = _entity(source)
    return StoredRelationship(
        id=UUID(edge_id),
        organization_id=source_entity.organization_id,
        workspace_id=_uuid(workspace_id),
        source=source_entity,
        target=_entity(target),
        relation_type=relation_type,
        sensitivity=sensitivity,
        source_memory_id=_uuid(memory_id),
        confidence=float(confidence),
        valid_from=_time(valid_from) or datetime.now(UTC),
        valid_until=_time(valid_until),
        metadata=json.loads(metadata) if metadata else {},
    )


class CypherGraphStore:
    """`GraphStore` over a Cypher database. The SQLAlchemy session argument is unused."""

    name = "cypher"

    def _run(self, query: str, **params: Any) -> list[list[Any]]:
        """Run one query; nodes in the returned rows are property mappings."""
        raise NotImplementedError

    def ping(self, session: Session) -> str:
        """Round-trip to the server; returns its version string."""
        raise NotImplementedError

    def _upsert_entity(
        self, organization_id: UUID, workspace_id: UUID | None, entity: ExtractedEntity
    ) -> str:
        rows = self._run(
            """
            MERGE (e:Entity {organization_id: $org, workspace_id: $ws,
                             canonical_name: $canonical, entity_type: $type})
            ON CREATE SET e.id = $id, e.name = $name, e.description = $description,
                          e.confidence = $confidence, e.created_at = $now, e.updated_at = $now
            ON MATCH SET e.confidence = CASE WHEN e.confidence < $confidence
                                             THEN $confidence ELSE e.confidence END,
                         e.description = CASE WHEN e.description = '' THEN $description
                                              ELSE e.description END,
                         e.updated_at = $now
            RETURN e.id
            """,
            org=str(organization_id),
            ws=_ws(workspace_id),
            canonical=_canonical(entity.name),
            type=entity.entity_type,
            id=str(uuid4()),
            name=entity.name,
            description=entity.description or "",
            confidence=float(entity.confidence),
            now=time.time(),
        )
        return rows[0][0]

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
        source_id = self._upsert_entity(organization_id, workspace_id, extracted.source)
        target_id = self._upsert_entity(organization_id, workspace_id, extracted.target)
        match = {
            "source": source_id,
            "target": target_id,
            "relation": extracted.relation_type,
            "sensitivity": sensitivity,
            "memory": str(source_memory_id) if source_memory_id else "",
        }
        open_edge = """
            MATCH (s:Entity {id: $source})-[r:REL]->(t:Entity {id: $target})
            WHERE r.relation_type = $relation AND r.sensitivity = $sensitivity
              AND r.source_memory_id = $memory AND r.valid_until IS NULL
        """
        existing = self._run(
            open_edge
            + """
            SET r.confidence = CASE WHEN r.confidence < $confidence
                                    THEN $confidence ELSE r.confidence END,
                r.updated_at = $now
            RETURN """
            + EDGE_FIELDS
            + " LIMIT 1",
            **match,
            confidence=float(extracted.confidence),
            now=time.time(),
        )
        if existing:
            return _stored(existing[0]), False
        details = dict(metadata or {})
        created = self._run(
            """
            MATCH (s:Entity {id: $source}), (t:Entity {id: $target})
            CREATE (s)-[r:REL {id: $id, organization_id: $org, workspace_id: $ws,
                               relation_type: $relation, sensitivity: $sensitivity,
                               source_memory_id: $memory, confidence: $confidence,
                               valid_from: $valid_from, valid_until: $valid_until,
                               metadata: $metadata, extracted: $extracted,
                               updated_at: $now}]->(t)
            RETURN """
            + EDGE_FIELDS,
            **match,
            id=str(uuid4()),
            org=str(organization_id),
            ws=_ws(workspace_id),
            confidence=float(extracted.confidence),
            valid_from=_epoch(valid_from),
            valid_until=_epoch(valid_until),
            metadata=json.dumps(details, default=str),
            extracted=bool(details.get("extracted")),
            now=time.time(),
        )
        return _stored(created[0]), True

    def close_memory_edges(
        self,
        session: Session,
        *,
        organization_id: UUID,
        memory_id: UUID,
        at: datetime,
        extracted_only: bool,
    ) -> int:
        rows = self._run(
            """
            MATCH (:Entity)-[r:REL]->(:Entity)
            WHERE r.organization_id = $org AND r.source_memory_id = $memory
              AND (r.valid_until IS NULL OR r.valid_until > $at)
              AND (NOT $extracted_only OR r.extracted)
            SET r.valid_until = $at, r.updated_at = $now
            RETURN count(r)
            """,
            org=str(organization_id),
            memory=str(memory_id),
            at=_epoch(at),
            extracted_only=extracted_only,
            now=time.time(),
        )
        return int(rows[0][0]) if rows else 0

    def retarget_memory_edges(
        self,
        session: Session,
        *,
        organization_id: UUID,
        memory_id: UUID,
        sensitivity: str,
        valid_until: datetime | None,
    ) -> int:
        rows = self._run(
            """
            MATCH (:Entity)-[r:REL]->(:Entity)
            WHERE r.organization_id = $org AND r.source_memory_id = $memory AND r.extracted
            SET r.sensitivity = $sensitivity, r.valid_until = $valid_until, r.updated_at = $now
            RETURN count(r)
            """,
            org=str(organization_id),
            memory=str(memory_id),
            sensitivity=sensitivity,
            valid_until=_epoch(valid_until),
            now=time.time(),
        )
        return int(rows[0][0]) if rows else 0

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
        terms = [term for term in re.findall(r"[\w.-]+", query.casefold()) if len(term) > 1][:10]
        if not terms:
            return []
        rows = self._run(
            f"""
            MATCH (s:Entity)-[r:REL]->(t:Entity)
            WHERE r.organization_id = $org AND r.sensitivity IN $sensitivities AND {VALID_AT}
              AND ($ws = '*' OR r.workspace_id = $ws)
              AND any(term IN $terms WHERE s.canonical_name CONTAINS term
                      OR t.canonical_name CONTAINS term
                      OR toLower(r.relation_type) CONTAINS term)
            RETURN {EDGE_FIELDS}
            ORDER BY r.confidence DESC, r.id
            LIMIT $limit
            """,
            org=str(organization_id),
            sensitivities=list(sensitivities),
            when=_epoch(temporal_as_of or datetime.now(UTC)),
            ws="*" if workspace_id is None else str(workspace_id),
            terms=terms,
            limit=int(limit),
        )
        return [_stored(row).read() for row in rows]

    def get_entity(
        self, session: Session, *, entity_id: UUID, organization_id: UUID | None = None
    ) -> GraphEntity | None:
        rows = self._run("MATCH (e:Entity {id: $id}) RETURN e LIMIT 1", id=str(entity_id))
        if not rows:
            return None
        entity = _entity(rows[0][0])
        if organization_id is not None and entity.organization_id != organization_id:
            return None
        return entity

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
        when = _epoch(temporal_as_of or datetime.now(UTC))
        frontier = {str(entity_id)}
        seen_entities = set(frontier)
        seen_edges: set[UUID] = set()
        results: list[RelationshipRead] = []
        for _ in range(hops):
            if not frontier or len(results) >= limit:
                break
            rows = self._run(
                f"""
                MATCH (s:Entity)-[r:REL]->(t:Entity)
                WHERE r.organization_id = $org AND r.workspace_id = $ws
                  AND r.sensitivity IN $sensitivities AND {VALID_AT}
                  AND (s.id IN $frontier OR t.id IN $frontier)
                RETURN {EDGE_FIELDS}
                ORDER BY r.id
                LIMIT $limit
                """,
                org=str(organization_id),
                ws=_ws(workspace_id),
                sensitivities=list(sensitivities),
                when=when,
                frontier=sorted(frontier),
                limit=int(limit - len(results)) + len(seen_edges),
            )
            reached: set[str] = set()
            for row in rows:
                edge = _stored(row)
                if edge.id in seen_edges or len(results) >= limit:
                    continue
                seen_edges.add(edge.id)
                results.append(edge.read())
                reached.update((str(edge.source.id), str(edge.target.id)))
            frontier = reached - seen_entities
            seen_entities.update(reached)
        return results

    def export_relationships(
        self,
        session: Session,
        *,
        organization_id: UUID,
        workspace_id: UUID | None,
        sensitivities: list[str],
    ) -> list[StoredRelationship]:
        rows = self._run(
            f"""
            MATCH (s:Entity)-[r:REL]->(t:Entity)
            WHERE r.organization_id = $org AND r.sensitivity IN $sensitivities
              AND NOT r.extracted AND ($ws = '*' OR r.workspace_id = $ws)
            RETURN {EDGE_FIELDS}
            ORDER BY r.id
            """,
            org=str(organization_id),
            sensitivities=list(sensitivities),
            ws="*" if workspace_id is None else str(workspace_id),
        )
        return [_stored(row) for row in rows]

    def snapshot(
        self,
        session: Session,
        *,
        organization_id: UUID,
        workspace_id: UUID | None,
        when: datetime,
        limit: int,
    ) -> tuple[list[GraphEntity], list[StoredRelationship], bool]:
        nodes = self._run(
            """
            MATCH (e:Entity)
            WHERE e.organization_id = $org AND ($ws = '*' OR e.workspace_id = $ws)
            RETURN e ORDER BY e.created_at DESC, e.id LIMIT $limit
            """,
            org=str(organization_id),
            ws="*" if workspace_id is None else str(workspace_id),
            limit=int(limit) + 1,
        )
        truncated = len(nodes) > limit
        entities = [_entity(row[0]) for row in nodes[:limit]]
        ids = [str(entity.id) for entity in entities]
        edges = (
            self._run(
                f"""
                MATCH (s:Entity)-[r:REL]->(t:Entity)
                WHERE r.organization_id = $org AND s.id IN $ids AND t.id IN $ids AND {VALID_AT}
                RETURN {EDGE_FIELDS}
                """,
                org=str(organization_id),
                ids=ids,
                when=_epoch(when),
            )
            if ids
            else []
        )
        return entities, [_stored(row) for row in edges], truncated

    def counts(self, session: Session, *, when: datetime) -> dict[UUID, tuple[int, int]]:
        entities = {
            org: int(count)
            for org, count in self._run("MATCH (e:Entity) RETURN e.organization_id, count(e)")
        }
        edges = {
            org: int(count)
            for org, count in self._run(
                f"MATCH ()-[r:REL]->() WHERE {VALID_AT} RETURN r.organization_id, count(r)",
                when=_epoch(when),
            )
        }
        return {UUID(org): (entities.get(org, 0), edges.get(org, 0)) for org in {*entities, *edges}}

    def fingerprint(self, session: Session, *, organization_id: UUID) -> str:
        org = str(organization_id)
        nodes = self._run(
            "MATCH (e:Entity) WHERE e.organization_id = $org RETURN count(e), max(e.updated_at)",
            org=org,
        )
        edges = self._run(
            "MATCH ()-[r:REL]->() WHERE r.organization_id = $org "
            "RETURN count(r), max(r.updated_at)",
            org=org,
        )
        return f"{nodes[0][0]}:{nodes[0][1]}|{edges[0][0]}:{edges[0][1]}"


INDEXED_PROPERTIES = ("id", "organization_id", "canonical_name")


class FalkorDBGraphStore(CypherGraphStore):
    name = "falkordb"

    def __init__(self, url: str, *, graph_name: str = "albert") -> None:
        try:
            from falkordb import FalkorDB
        except ImportError as exc:  # pragma: no cover - exercised only without the package
            raise RuntimeError(
                "ALBERT_GRAPH_STORE=falkordb needs the 'falkordb' Python package installed"
            ) from exc
        parsed = urlparse(url)
        self._db = FalkorDB(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            username=parsed.username or None,
            password=unquote(parsed.password) if parsed.password else None,
            socket_connect_timeout=5,
            socket_timeout=30,
        )
        self._graph = self._db.select_graph(graph_name)
        for name in INDEXED_PROPERTIES:
            try:
                self._graph.create_node_range_index("Entity", name)
            except Exception as exc:  # the index already exists
                if "already indexed" not in str(exc).lower():
                    raise

    def _run(self, query: str, **params: Any) -> list[list[Any]]:
        rows = self._graph.query(query, params).result_set
        return [
            [value.properties if hasattr(value, "properties") else value for value in row]
            for row in rows
        ]

    def ping(self, session: Session) -> str:
        modules = self._db.connection.execute_command("MODULE", "LIST")
        for module in modules:
            fields = dict(zip(module[::2], module[1::2], strict=False))
            name = fields.get("name", fields.get(b"name"))
            if name in ("graph", b"graph"):
                version = int(fields.get("ver", fields.get(b"ver", 0)))
                return f"{version // 10000}.{version // 100 % 100}.{version % 100}"
        return "unknown"


class Neo4jGraphStore(CypherGraphStore):
    name = "neo4j"

    def __init__(self, url: str, *, user: str, password: str, database: str = "neo4j") -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - exercised only without the package
            raise RuntimeError(
                "ALBERT_GRAPH_STORE=neo4j needs the 'neo4j' Python package installed"
            ) from exc
        self._driver = GraphDatabase.driver(url, auth=(user, password), connection_timeout=5)
        self._database = database
        for name in INDEXED_PROPERTIES:
            self._run(
                f"CREATE INDEX albert_entity_{name} IF NOT EXISTS FOR (e:Entity) ON (e.{name})"
            )

    def _run(self, query: str, **params: Any) -> list[list[Any]]:
        records, _summary, _keys = self._driver.execute_query(
            query, params, database_=self._database
        )
        return [
            [dict(value) if hasattr(value, "labels") else value for value in record.values()]
            for record in records
        ]

    def ping(self, session: Session) -> str:
        rows = self._run("CALL dbms.components() YIELD versions RETURN versions[0]")
        return str(rows[0][0]) if rows else "unknown"

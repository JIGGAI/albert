from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from albert.classifier import ExtractedEntity, ExtractedRelationship
from albert.models import Entity, Relationship
from albert.schemas import RelationshipRead


def canonical_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def upsert_entity(
    session: Session,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    entity: ExtractedEntity,
) -> Entity:
    canonical = canonical_name(entity.name)
    existing = session.scalar(
        select(Entity).where(
            Entity.organization_id == organization_id,
            Entity.workspace_id == workspace_id,
            Entity.canonical_name == canonical,
            Entity.entity_type == entity.entity_type,
        )
    )
    if existing is not None:
        if entity.description and not existing.description:
            existing.description = entity.description
        existing.confidence = max(existing.confidence, entity.confidence)
        return existing
    record = Entity(
        organization_id=organization_id,
        workspace_id=workspace_id,
        canonical_name=canonical,
        display_name=entity.name,
        entity_type=entity.entity_type,
        description=entity.description,
        confidence=entity.confidence,
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
        return record
    except IntegrityError:
        existing = session.scalar(
            select(Entity).where(
                Entity.organization_id == organization_id,
                Entity.workspace_id == workspace_id,
                Entity.canonical_name == canonical,
                Entity.entity_type == entity.entity_type,
            )
        )
        if existing is None:
            raise
        return existing


def add_relationship(
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
) -> tuple[Relationship, bool]:
    """Create or reuse an open edge; returns (relationship, created)."""
    source = upsert_entity(
        session,
        organization_id=organization_id,
        workspace_id=workspace_id,
        entity=extracted.source,
    )
    target = upsert_entity(
        session,
        organization_id=organization_id,
        workspace_id=workspace_id,
        entity=extracted.target,
    )
    existing = session.scalar(
        select(Relationship).where(
            Relationship.organization_id == organization_id,
            Relationship.workspace_id == workspace_id,
            Relationship.source_entity_id == source.id,
            Relationship.target_entity_id == target.id,
            Relationship.relation_type == extracted.relation_type,
            Relationship.sensitivity == sensitivity,
            Relationship.source_memory_id == source_memory_id,
            Relationship.valid_until.is_(None),
        )
    )
    if existing is not None:
        existing.confidence = max(existing.confidence, extracted.confidence)
        return existing, False
    relationship = Relationship(
        organization_id=organization_id,
        workspace_id=workspace_id,
        source_entity_id=source.id,
        target_entity_id=target.id,
        relation_type=extracted.relation_type,
        sensitivity=sensitivity,
        source_memory_id=source_memory_id,
        confidence=extracted.confidence,
        valid_from=valid_from,
        valid_until=valid_until,
        metadata_=metadata or {},
    )
    session.add(relationship)
    session.flush()
    return relationship, True


def query_relationships(
    session: Session,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    query: str,
    temporal_as_of: datetime | None,
    limit: int,
    sensitivities: list[str],
) -> list[RelationshipRead]:
    source = aliased(Entity)
    target = aliased(Entity)
    when = temporal_as_of or datetime.now(UTC)
    conditions = [
        Relationship.organization_id == organization_id,
        Relationship.sensitivity.in_(sensitivities),
        Relationship.valid_from <= when,
        or_(Relationship.valid_until.is_(None), Relationship.valid_until > when),
    ]
    if workspace_id is not None:
        conditions.append(Relationship.workspace_id == workspace_id)
    statement = (
        select(Relationship, source, target)
        .join(source, Relationship.source_entity_id == source.id)
        .join(target, Relationship.target_entity_id == target.id)
        .where(*conditions)
    )
    terms = [term for term in re.findall(r"[\w.-]+", query.casefold()) if len(term) > 1]
    if not terms:
        return []
    clauses = []
    for term in terms[:10]:
        pattern = f"%{term}%"
        clauses.extend(
            [
                source.canonical_name.ilike(pattern),
                target.canonical_name.ilike(pattern),
                Relationship.relation_type.ilike(pattern),
            ]
        )
    statement = statement.where(or_(*clauses))
    rows = session.execute(statement.order_by(Relationship.confidence.desc()).limit(limit)).all()
    return [
        RelationshipRead(
            id=relationship.id,
            source_entity_id=source_entity.id,
            source_name=source_entity.display_name,
            source_type=source_entity.entity_type,
            relation_type=relationship.relation_type,
            sensitivity=relationship.sensitivity,
            target_entity_id=target_entity.id,
            target_name=target_entity.display_name,
            target_type=target_entity.entity_type,
            source_memory_id=relationship.source_memory_id,
            confidence=relationship.confidence,
            valid_from=relationship.valid_from,
            valid_until=relationship.valid_until,
        )
        for relationship, source_entity, target_entity in rows
    ]


def subgraph(
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
    frontier = {entity_id}
    seen_entities = {entity_id}
    seen_relationships: set[UUID] = set()
    results: list[RelationshipRead] = []
    when = temporal_as_of or datetime.now(UTC)
    source = aliased(Entity)
    target = aliased(Entity)
    for _ in range(hops):
        if not frontier or len(results) >= limit:
            break
        rows = session.execute(
            select(Relationship, source, target)
            .join(source, Relationship.source_entity_id == source.id)
            .join(target, Relationship.target_entity_id == target.id)
            .where(
                Relationship.organization_id == organization_id,
                Relationship.workspace_id == workspace_id,
                Relationship.sensitivity.in_(sensitivities),
                Relationship.valid_from <= when,
                or_(Relationship.valid_until.is_(None), Relationship.valid_until > when),
                or_(
                    Relationship.source_entity_id.in_(frontier),
                    Relationship.target_entity_id.in_(frontier),
                ),
            )
            .limit(limit - len(results))
        ).all()
        next_frontier: set[UUID] = set()
        for relationship, source_entity, target_entity in rows:
            if relationship.id in seen_relationships:
                continue
            seen_relationships.add(relationship.id)
            results.append(
                RelationshipRead(
                    id=relationship.id,
                    source_entity_id=source_entity.id,
                    source_name=source_entity.display_name,
                    source_type=source_entity.entity_type,
                    relation_type=relationship.relation_type,
                    sensitivity=relationship.sensitivity,
                    target_entity_id=target_entity.id,
                    target_name=target_entity.display_name,
                    target_type=target_entity.entity_type,
                    source_memory_id=relationship.source_memory_id,
                    confidence=relationship.confidence,
                    valid_from=relationship.valid_from,
                    valid_until=relationship.valid_until,
                )
            )
            next_frontier.update((source_entity.id, target_entity.id))
        frontier = next_frontier - seen_entities
        seen_entities.update(next_frontier)
    return results

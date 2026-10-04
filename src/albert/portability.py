from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from albert.classifier import ExtractedEntity, ExtractedRelationship
from albert.graph_store import get_graph_store
from albert.models import Memory, Organization, Workspace
from albert.schemas import (
    EntityInput,
    ExportBundle,
    ImportRequest,
    ImportResult,
    MemoryExport,
    RelationshipExport,
    WorkspaceExport,
)
from albert.security import AuthContext
from albert.services import audit, enqueue, reject_secrets, resolve_workspace


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def export_bundle(session: Session, auth: AuthContext, workspace_id: UUID | None) -> ExportBundle:
    auth.require("memory.export")
    selected_workspace = resolve_workspace(session, auth, workspace_id)
    sensitivities = auth.allowed_sensitivities()
    memory_conditions = [
        Memory.organization_id == auth.organization_id,
        Memory.status == "active",
        Memory.sensitivity.in_(sensitivities),
    ]
    if selected_workspace is not None:
        memory_conditions.append(Memory.workspace_id == selected_workspace)

    memories = list(session.scalars(select(Memory).where(*memory_conditions)))
    relationships = get_graph_store().export_relationships(
        session,
        organization_id=auth.organization_id,
        workspace_id=selected_workspace,
        sensitivities=sensitivities,
    )
    workspace_ids = {
        item.workspace_id for item in [*memories, *relationships] if item.workspace_id is not None
    }
    if selected_workspace is not None:
        workspace_ids.add(selected_workspace)
    workspaces = (
        list(
            session.scalars(
                select(Workspace).where(
                    Workspace.organization_id == auth.organization_id,
                    Workspace.id.in_(workspace_ids),
                )
            )
        )
        if workspace_ids
        else []
    )
    organization = session.get(Organization, auth.organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    exported_memory_ids = {memory.id for memory in memories}
    bundle = ExportBundle(
        exported_at=datetime.now(UTC),
        organization_name=organization.name,
        workspaces=[WorkspaceExport(ref=item.id, name=item.name) for item in workspaces],
        memories=[
            MemoryExport(
                ref=memory.id,
                workspace_ref=memory.workspace_id,
                project_ref=memory.project_ref,
                task_ref=memory.task_ref,
                run_ref=memory.run_ref,
                subject=memory.subject,
                content=memory.content,
                memory_type=memory.memory_type,
                sensitivity=memory.sensitivity,  # type: ignore[arg-type]
                confidence=memory.confidence,
                valid_from=memory.valid_from,
                valid_until=memory.valid_until,
                metadata=memory.metadata_ or {},
            )
            for memory in memories
        ],
        relationships=[
            RelationshipExport(
                workspace_ref=relationship.workspace_id,
                source=EntityInput(
                    name=relationship.source.name,
                    entity_type=relationship.source.entity_type,
                    description=relationship.source.description,
                    confidence=relationship.source.confidence,
                ),
                relation_type=relationship.relation_type,
                target=EntityInput(
                    name=relationship.target.name,
                    entity_type=relationship.target.entity_type,
                    description=relationship.target.description,
                    confidence=relationship.target.confidence,
                ),
                sensitivity=relationship.sensitivity,  # type: ignore[arg-type]
                source_memory_ref=(
                    relationship.source_memory_id
                    if relationship.source_memory_id in exported_memory_ids
                    else None
                ),
                confidence=relationship.confidence,
                valid_from=relationship.valid_from,
                valid_until=relationship.valid_until,
                metadata=relationship.metadata,
            )
            for relationship in relationships
        ],
    )
    audit(
        session,
        auth,
        "memory.exported",
        detail={
            "memories": len(bundle.memories),
            "relationships": len(bundle.relationships),
        },
    )
    session.commit()
    return bundle


def import_bundle(session: Session, auth: AuthContext, request: ImportRequest) -> ImportResult:
    auth.require("memory.import")
    destination = resolve_workspace(session, auth, request.workspace_id)
    workspace_map: dict[UUID, UUID | None] = {}
    workspaces_created = 0

    if destination is not None:
        workspace_map = {item.ref: destination for item in request.bundle.workspaces}
    else:
        if auth.workspace_id is not None:
            raise HTTPException(
                status_code=403, detail="Workspace-scoped import requires a destination"
            )
        for source_workspace in request.bundle.workspaces:
            workspace = session.scalar(
                select(Workspace).where(
                    Workspace.organization_id == auth.organization_id,
                    Workspace.name == source_workspace.name,
                )
            )
            if workspace is None:
                workspace = Workspace(
                    organization_id=auth.organization_id,
                    name=source_workspace.name,
                )
                session.add(workspace)
                session.flush()
                workspaces_created += 1
            workspace_map[source_workspace.ref] = workspace.id

    def mapped_workspace(source_ref: UUID | None) -> UUID | None:
        if destination is not None:
            return destination
        if source_ref is None:
            return None
        if source_ref not in workspace_map:
            raise HTTPException(
                status_code=422, detail="Bundle contains an unknown workspace reference"
            )
        return workspace_map[source_ref]

    allowed_sensitivities = set(auth.allowed_sensitivities())
    memory_map: dict[UUID, UUID] = {}
    memories_created = 0
    memories_skipped = 0
    for item in request.bundle.memories:
        if item.sensitivity not in allowed_sensitivities:
            raise HTTPException(status_code=403, detail="Bundle contains unauthorized sensitivity")
        reject_secrets(item.subject, item.content, item.metadata, what="Bundle")
        if item.valid_until is not None and _as_utc(item.valid_until) <= _as_utc(item.valid_from):
            raise HTTPException(status_code=422, detail="Memory validity interval is invalid")
        already_imported = session.scalar(
            select(Memory).where(
                Memory.organization_id == auth.organization_id,
                Memory.status == "active",
                Memory.metadata_["imported_from"]["organization"].as_string()
                == request.bundle.organization_name,
                Memory.metadata_["imported_from"]["memory_ref"].as_string() == str(item.ref),
            )
        )
        if already_imported is not None:
            memory_map[item.ref] = already_imported.id
            memories_skipped += 1
            continue
        memory = Memory(
            organization_id=auth.organization_id,
            workspace_id=mapped_workspace(item.workspace_ref),
            project_ref=item.project_ref,
            task_ref=item.task_ref,
            run_ref=item.run_ref,
            owner_principal_id=auth.principal_id,
            subject=item.subject,
            content=item.content,
            memory_type=item.memory_type,
            sensitivity=item.sensitivity,
            confidence=item.confidence,
            valid_from=item.valid_from,
            valid_until=item.valid_until,
            metadata_={
                **item.metadata,
                "imported_from": {
                    "organization": request.bundle.organization_name,
                    "memory_ref": str(item.ref),
                    "exported_at": request.bundle.exported_at.isoformat(),
                },
            },
        )
        session.add(memory)
        session.flush()
        memory_map[item.ref] = memory.id
        memories_created += 1
        enqueue(session, auth.organization_id, "enrich_memory", {"memory_id": str(memory.id)})

    relationships_created = 0
    for item in request.bundle.relationships:
        if item.sensitivity not in allowed_sensitivities:
            raise HTTPException(status_code=403, detail="Bundle contains unauthorized sensitivity")
        reject_secrets(
            item.source.name,
            item.source.description,
            item.target.name,
            item.target.description,
            item.metadata,
            what="Bundle",
        )
        if item.valid_until is not None and _as_utc(item.valid_until) <= _as_utc(item.valid_from):
            raise HTTPException(status_code=422, detail="Relationship validity interval is invalid")
        source_memory_id = None
        if item.source_memory_ref is not None:
            source_memory_id = memory_map.get(item.source_memory_ref)
            if source_memory_id is None:
                raise HTTPException(
                    status_code=422, detail="Relationship references an unknown memory"
                )
        _relationship, created = get_graph_store().add_relationship(
            session,
            organization_id=auth.organization_id,
            workspace_id=mapped_workspace(item.workspace_ref),
            extracted=ExtractedRelationship(
                source=ExtractedEntity(
                    name=item.source.name,
                    entity_type=item.source.entity_type,
                    description=item.source.description,
                    confidence=item.source.confidence,
                ),
                relation_type=item.relation_type,
                target=ExtractedEntity(
                    name=item.target.name,
                    entity_type=item.target.entity_type,
                    description=item.target.description,
                    confidence=item.target.confidence,
                ),
                confidence=item.confidence,
            ),
            source_memory_id=source_memory_id,
            sensitivity=item.sensitivity,
            valid_from=item.valid_from,
            valid_until=item.valid_until,
            metadata={**item.metadata, "imported": True},
        )
        relationships_created += int(created)

    result = ImportResult(
        memories_created=memories_created,
        memories_skipped=memories_skipped,
        relationships_created=relationships_created,
        workspaces_created=workspaces_created,
    )
    audit(session, auth, "memory.imported", detail=result.model_dump())
    session.commit()
    return result

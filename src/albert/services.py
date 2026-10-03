from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from albert.chunking import chunk_text
from albert.classifier import (
    ExtractedEntity,
    ExtractedRelationship,
    classify,
    contains_likely_secret,
)
from albert.config import get_settings
from albert.embeddings import get_embedder
from albert.graph import add_relationship
from albert.models import (
    AuditEvent,
    Episode,
    Job,
    Memory,
    MemoryChunk,
    Relationship,
    ResourceLock,
    WorkingMemory,
    Workspace,
    utcnow,
)
from albert.recorder import span
from albert.schemas import (
    EpisodeCreate,
    LockAcquire,
    MemoryCreate,
    MemoryUpdate,
    RelationshipCreate,
    WorkingMemoryCreate,
    WorkingMemoryFinish,
    WorkingMemoryUpdate,
)
from albert.security import AuthContext, hash_lock_token, verify_lock_token

SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _highest_sensitivity(*values: str) -> str:
    """Classification may raise a record's sensitivity but never lower it."""
    return max(values, key=SENSITIVITY_ORDER.index)


def reject_secrets(*parts: str | dict | None, what: str = "Content") -> None:
    """Refuse to persist anything that looks like a credential.

    Every text or JSON field Albert stores or exports goes through here, not
    only memory bodies: working-memory notes, graph descriptions and metadata
    are retrievable too.
    """
    for part in parts:
        if part is None:
            continue
        text = part if isinstance(part, str) else json.dumps(part, default=str)
        if contains_likely_secret(text):
            raise HTTPException(
                status_code=422, detail=f"{what} appears to contain a credential or secret"
            )


def resolve_workspace(session: Session, auth: AuthContext, requested: UUID | None) -> UUID | None:
    with span("resolve_scope", requested=str(requested) if requested else None) as handle:
        if auth.workspace_id is not None and requested not in (None, auth.workspace_id):
            raise HTTPException(status_code=403, detail="Workspace is outside the API key scope")
        workspace_id = requested if requested is not None else auth.workspace_id
        if workspace_id is not None:
            exists = session.scalar(
                select(Workspace.id).where(
                    Workspace.id == workspace_id,
                    Workspace.organization_id == auth.organization_id,
                )
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
        handle.set(workspace_id=str(workspace_id) if workspace_id else None)
        return workspace_id


def audit(
    session: Session,
    auth: AuthContext,
    event_type: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict | None = None,
) -> None:
    session.add(
        AuditEvent(
            organization_id=auth.organization_id,
            workspace_id=auth.workspace_id,
            principal_id=auth.principal_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail or {},
        )
    )


def enqueue(session: Session, organization_id: UUID, job_type: str, payload: dict) -> Job:
    job = Job(organization_id=organization_id, job_type=job_type, payload=payload)
    session.add(job)
    return job


def create_episode(
    session: Session, auth: AuthContext, data: EpisodeCreate
) -> tuple[Episode, bool]:
    """Persist a canonical episode; returns (episode, created).

    Deduplication is scoped to the calling principal so one principal can never
    learn about, or be handed, another principal's episode by re-posting text.
    """
    auth.require("memory.write")
    reject_secrets(data.content, data.metadata)
    workspace_id = resolve_workspace(session, auth, data.workspace_id)
    digest = hashlib.sha256(data.content.encode()).hexdigest()
    duplicate_of = select(Episode).where(
        Episode.organization_id == auth.organization_id,
        Episode.workspace_id == workspace_id,
        Episode.owner_principal_id == auth.principal_id,
        Episode.content_hash == digest,
        Episode.source_uri == data.source_uri,
    )
    existing = session.scalar(duplicate_of)
    if existing is not None:
        return existing, False
    episode = Episode(
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        project_ref=data.project_ref,
        task_ref=data.task_ref,
        run_ref=data.run_ref,
        owner_principal_id=auth.principal_id,
        source_type=data.source_type,
        source_uri=data.source_uri,
        content=data.content,
        content_hash=digest,
        sensitivity=data.sensitivity,
        occurred_at=data.occurred_at or utcnow(),
        extra=data.metadata,
    )
    session.add(episode)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        existing = session.scalar(duplicate_of)
        if existing is not None:
            return existing, False
        raise HTTPException(status_code=409, detail="Episode was concurrently ingested") from exc
    enqueue(session, auth.organization_id, "enrich_episode", {"episode_id": str(episode.id)})
    audit(session, auth, "episode.created", resource_type="episode", resource_id=str(episode.id))
    session.commit()
    session.refresh(episode)
    return episode, True


def create_memory(session: Session, auth: AuthContext, data: MemoryCreate) -> Memory:
    auth.require("memory.write")
    reject_secrets(data.subject, data.content, data.metadata)
    workspace_id = resolve_workspace(session, auth, data.workspace_id)
    memory = Memory(
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        project_ref=data.project_ref,
        task_ref=data.task_ref,
        run_ref=data.run_ref,
        owner_principal_id=auth.principal_id,
        subject=data.subject,
        content=data.content,
        memory_type=data.memory_type,
        sensitivity=data.sensitivity,
        confidence=data.confidence,
        valid_from=data.valid_from or utcnow(),
        valid_until=data.valid_until,
        metadata_=data.metadata,
    )
    session.add(memory)
    session.flush()
    enqueue(session, auth.organization_id, "enrich_memory", {"memory_id": str(memory.id)})
    audit(session, auth, "memory.created", resource_type="memory", resource_id=str(memory.id))
    session.commit()
    session.refresh(memory)
    return memory


def get_memory(session: Session, auth: AuthContext, memory_id: UUID) -> Memory:
    auth.require("memory.read")
    statement = select(Memory).where(
        Memory.id == memory_id,
        Memory.organization_id == auth.organization_id,
        Memory.status != "deleted",
    )
    if auth.workspace_id is not None:
        statement = statement.where(Memory.workspace_id == auth.workspace_id)
    memory = session.scalar(statement)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    if memory.sensitivity not in auth.allowed_sensitivities():
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


def update_memory(
    session: Session, auth: AuthContext, memory_id: UUID, data: MemoryUpdate
) -> Memory:
    auth.require("memory.write")
    memory = get_memory(session, auth, memory_id)
    changes = data.model_dump(exclude_unset=True)
    if "valid_until" in changes:
        valid_until = changes["valid_until"]
        if valid_until is not None and _as_utc(valid_until) <= _as_utc(memory.valid_from):
            raise HTTPException(status_code=422, detail="valid_until must be after valid_from")
    metadata = changes.pop("metadata", None)
    reject_secrets(changes.get("subject"), changes.get("content"), metadata)
    for name, value in changes.items():
        setattr(memory, name, value)
    if metadata is not None:
        memory.metadata_ = metadata
    text_changed = data.content is not None or data.subject is not None
    extracted_edges = [
        relationship
        for relationship in session.scalars(
            select(Relationship).where(
                Relationship.organization_id == auth.organization_id,
                Relationship.source_memory_id == memory.id,
            )
        )
        if (relationship.metadata_ or {}).get("extracted")
    ]
    if text_changed:
        # Old chunks describe text that no longer exists; drop them now rather
        # than serve them from vector search until the worker catches up.
        _drop_chunks(session, memory.id)
        memory.embedding_model = None
        # The facts themselves may have changed: close the old edges now and let
        # re-enrichment extract the current ones.
        now = utcnow()
        for relationship in extracted_edges:
            if relationship.valid_until is None or _as_utc(relationship.valid_until) > now:
                relationship.valid_until = now
        enqueue(session, auth.organization_id, "enrich_memory", {"memory_id": str(memory.id)})
    elif {"sensitivity", "valid_until"}.intersection(changes):
        # The facts are unchanged; their visibility window follows the memory.
        for relationship in extracted_edges:
            relationship.sensitivity = memory.sensitivity
            relationship.valid_until = memory.valid_until
    audit(session, auth, "memory.updated", resource_type="memory", resource_id=str(memory.id))
    session.commit()
    session.refresh(memory)
    return memory


def _classify_traced(text: str):  # type: ignore[no-untyped-def]
    with span("classify", provider=get_settings().llm_provider) as handle:
        result = classify(text)
        handle.set(
            memory_type=result.memory_type,
            sensitivity=result.sensitivity,
            confidence=result.confidence,
            entities=len(result.entities),
            relationships=len(result.relationships),
        )
        return result


def _drop_chunks(session: Session, memory_id: UUID) -> None:
    session.query(MemoryChunk).filter(MemoryChunk.memory_id == memory_id).delete(
        synchronize_session=False
    )


def index_memory_chunks(session: Session, memory: Memory) -> None:
    """Replace a memory's chunks and embeddings with ones for its current text."""
    settings = get_settings()
    embedder = get_embedder()
    _drop_chunks(session, memory.id)
    with span("chunk", max_characters=settings.chunk_characters) as chunk_span:
        chunks = chunk_text(
            memory.content,
            max_characters=settings.chunk_characters,
            overlap_characters=settings.chunk_overlap_characters,
        )
        truncated = len(chunks) > settings.max_chunks_per_memory
        chunks = chunks[: settings.max_chunks_per_memory]
        chunk_span.set(chunks=len(chunks), truncated=truncated)
    with span("embed", embedding_model=embedder.name, dimensions=embedder.dimensions) as embed_span:
        for index, chunk in enumerate(chunks):
            text = f"{memory.subject}\n{chunk}" if memory.subject else chunk
            session.add(
                MemoryChunk(
                    memory_id=memory.id,
                    organization_id=memory.organization_id,
                    workspace_id=memory.workspace_id,
                    chunk_index=index,
                    content=chunk,
                    embedding=embedder.embed(text),
                    embedding_model=embedder.name,
                )
            )
        embed_span.set(vectors=len(chunks))
    memory.embedding_model = embedder.name
    memory.metadata_ = {
        **(memory.metadata_ or {}),
        "embedding": {"model": embedder.name, "chunks": len(chunks), "truncated": truncated},
    }


def _scrub_memory(session: Session, memory: Memory, now: datetime) -> None:
    """Forget a memory: keep the row for audit identity, drop everything it said."""
    memory.status = "deleted"
    memory.subject = ""
    memory.content = ""
    memory.embedding_model = None
    memory.metadata_ = {}
    _drop_chunks(session, memory.id)
    session.query(Relationship).filter(
        Relationship.organization_id == memory.organization_id,
        Relationship.source_memory_id == memory.id,
        or_(Relationship.valid_until.is_(None), Relationship.valid_until > now),
    ).update({Relationship.valid_until: now}, synchronize_session=False)


def delete_memory(session: Session, auth: AuthContext, memory_id: UUID) -> None:
    auth.require("memory.delete")
    memory = get_memory(session, auth, memory_id)
    _scrub_memory(session, memory, utcnow())
    audit(session, auth, "memory.deleted", resource_type="memory", resource_id=str(memory.id))
    session.commit()


def get_episode(session: Session, auth: AuthContext, episode_id: UUID) -> Episode:
    auth.require("memory.read")
    statement = select(Episode).where(
        Episode.id == episode_id,
        Episode.organization_id == auth.organization_id,
        Episode.deleted_at.is_(None),
    )
    if auth.workspace_id is not None:
        statement = statement.where(Episode.workspace_id == auth.workspace_id)
    episode = session.scalar(statement)
    if episode is None or episode.sensitivity not in auth.allowed_sensitivities():
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


def delete_episode(session: Session, auth: AuthContext, episode_id: UUID) -> None:
    """Delete a canonical episode together with everything derived from it."""
    auth.require("memory.delete")
    episode = get_episode(session, auth, episode_id)
    now = utcnow()
    for memory in session.scalars(
        select(Memory).where(Memory.episode_id == episode.id, Memory.status != "deleted")
    ):
        _scrub_memory(session, memory, now)
    episode.content = ""
    episode.extra = {}
    episode.enrichment_error = None
    episode.deleted_at = now
    audit(session, auth, "episode.deleted", resource_type="episode", resource_id=str(episode.id))
    session.commit()


def add_explicit_relationship(
    session: Session, auth: AuthContext, data: RelationshipCreate
) -> Relationship:
    auth.require("graph.write")
    reject_secrets(
        data.source.name,
        data.source.description,
        data.target.name,
        data.target.description,
        data.metadata,
        what="Relationship",
    )
    workspace_id = resolve_workspace(session, auth, data.workspace_id)
    if data.source_memory_id is not None:
        get_memory(session, auth, data.source_memory_id)
    extracted = ExtractedRelationship(
        source=ExtractedEntity(
            data.source.name,
            data.source.entity_type,
            data.source.description,
            data.source.confidence,
        ),
        relation_type=data.relation_type,
        target=ExtractedEntity(
            data.target.name,
            data.target.entity_type,
            data.target.description,
            data.target.confidence,
        ),
        confidence=data.confidence,
    )
    relationship, _created = add_relationship(
        session,
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        extracted=extracted,
        source_memory_id=data.source_memory_id,
        sensitivity=data.sensitivity,
        valid_from=data.valid_from or utcnow(),
        valid_until=data.valid_until,
        metadata=data.metadata,
    )
    audit(
        session,
        auth,
        "relationship.created",
        resource_type="relationship",
        resource_id=str(relationship.id),
    )
    session.commit()
    session.refresh(relationship)
    return relationship


def create_working_memory(
    session: Session, auth: AuthContext, data: WorkingMemoryCreate
) -> tuple[WorkingMemory, tuple[ResourceLock, str] | None]:
    auth.require("working_memory.write")
    reject_secrets(data.description, what="Working memory")
    workspace_id = resolve_workspace(session, auth, data.workspace_id)
    working = WorkingMemory(
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        owner_principal_id=auth.principal_id,
        task_id=data.task_id,
        description=data.description,
        resource_type=data.resource_type,
        resource_ref=data.resource_ref,
        expires_at=utcnow() + timedelta(seconds=data.expires_in_seconds),
    )
    session.add(working)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Task ID already exists") from exc
    lock = None
    if data.acquire_lock:
        if not data.resource_type or not data.resource_ref:
            raise HTTPException(status_code=422, detail="Resource type and reference are required")
        lock = acquire_lock(
            session,
            auth,
            LockAcquire(
                workspace_id=workspace_id,
                working_memory_id=working.id,
                resource_type=data.resource_type,
                resource_ref=data.resource_ref,
                ttl_seconds=min(data.expires_in_seconds, 86400),
            ),
            commit=False,
        )
    audit(
        session,
        auth,
        "working_memory.created",
        resource_type="working_memory",
        resource_id=str(working.id),
    )
    session.commit()
    session.refresh(working)
    return working, lock


def get_working_memory(session: Session, auth: AuthContext, working_id: UUID) -> WorkingMemory:
    statement = select(WorkingMemory).where(
        WorkingMemory.id == working_id,
        WorkingMemory.organization_id == auth.organization_id,
    )
    if auth.workspace_id is not None:
        statement = statement.where(WorkingMemory.workspace_id == auth.workspace_id)
    working = session.scalar(statement)
    if working is None:
        raise HTTPException(status_code=404, detail="Working memory not found")
    return working


def update_working_memory(
    session: Session, auth: AuthContext, working_id: UUID, data: WorkingMemoryUpdate
) -> WorkingMemory:
    auth.require("working_memory.write")
    working = get_working_memory(session, auth, working_id)
    if working.owner_principal_id != auth.principal_id and "admin" not in auth.capabilities:
        raise HTTPException(status_code=403, detail="Working memory is owned by another principal")
    if working.status != "active":
        raise HTTPException(status_code=409, detail="Working memory is not active")
    reject_secrets(data.progress, what="Working memory progress")
    if data.progress is not None:
        working.progress = data.progress
    if data.expires_in_seconds is not None:
        working.expires_at = utcnow() + timedelta(seconds=data.expires_in_seconds)
    session.commit()
    session.refresh(working)
    return working


def finish_working_memory(
    session: Session,
    auth: AuthContext,
    working_id: UUID,
    data: WorkingMemoryFinish,
    *,
    failed: bool,
) -> WorkingMemory:
    auth.require("working_memory.write")
    working = get_working_memory(session, auth, working_id)
    if working.owner_principal_id != auth.principal_id and "admin" not in auth.capabilities:
        raise HTTPException(status_code=403, detail="Working memory is owned by another principal")
    if working.status != "active":
        raise HTTPException(status_code=409, detail="Working memory is not active")
    reject_secrets(data.consolidation_notes, what="Consolidation notes")
    working.status = "failed" if failed else "completed"
    working.completed_at = utcnow()
    working.consolidation_notes = data.consolidation_notes
    session.query(ResourceLock).filter(
        ResourceLock.organization_id == auth.organization_id,
        ResourceLock.working_memory_id == working.id,
        ResourceLock.released_at.is_(None),
    ).update({ResourceLock.released_at: working.completed_at})
    audit(
        session,
        auth,
        f"working_memory.{working.status}",
        resource_type="working_memory",
        resource_id=str(working.id),
    )
    session.commit()
    session.refresh(working)
    return working


def acquire_lock(
    session: Session, auth: AuthContext, data: LockAcquire, *, commit: bool = True
) -> tuple[ResourceLock, str]:
    auth.require("locks.acquire")
    working = None
    if data.working_memory_id is not None:
        working = session.scalar(
            select(WorkingMemory).where(
                WorkingMemory.id == data.working_memory_id,
                WorkingMemory.organization_id == auth.organization_id,
            )
        )
        if working is None:
            raise HTTPException(status_code=404, detail="Working memory not found")
        if working.owner_principal_id != auth.principal_id and "admin" not in auth.capabilities:
            raise HTTPException(
                status_code=403, detail="Working memory is owned by another principal"
            )
        if working.status != "active" or _as_utc(working.expires_at) <= utcnow():
            raise HTTPException(status_code=409, detail="Working memory is not active")
        if working.resource_type and working.resource_type != data.resource_type:
            raise HTTPException(status_code=409, detail="Lock resource type does not match task")
        if working.resource_ref and working.resource_ref != data.resource_ref:
            raise HTTPException(
                status_code=409, detail="Lock resource reference does not match task"
            )
    requested_workspace = data.workspace_id
    if requested_workspace is None and working is not None:
        requested_workspace = working.workspace_id
    workspace_id = resolve_workspace(session, auth, requested_workspace)
    if working is not None and working.workspace_id != workspace_id:
        raise HTTPException(status_code=409, detail="Lock workspace does not match task")
    now = utcnow()
    statement = select(ResourceLock).where(
        ResourceLock.organization_id == auth.organization_id,
        ResourceLock.resource_type == data.resource_type,
        ResourceLock.resource_ref == data.resource_ref,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    existing = session.scalar(statement)
    if existing is not None and existing.released_at is None and _as_utc(existing.expires_at) > now:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Resource is locked",
                "owner_principal_id": str(existing.owner_principal_id),
                "expires_at": existing.expires_at.isoformat(),
            },
        )
    token = secrets.token_urlsafe(32)
    if existing is None:
        lock = ResourceLock(
            organization_id=auth.organization_id,
            workspace_id=workspace_id,
            owner_principal_id=auth.principal_id,
            working_memory_id=data.working_memory_id,
            resource_type=data.resource_type,
            resource_ref=data.resource_ref,
            fence=1,
            token_hash=hash_lock_token(token),
            expires_at=now + timedelta(seconds=data.ttl_seconds),
        )
        session.add(lock)
    else:
        lock = existing
        lock.workspace_id = workspace_id
        lock.owner_principal_id = auth.principal_id
        lock.working_memory_id = data.working_memory_id
        lock.fence += 1
        lock.token_hash = hash_lock_token(token)
        lock.expires_at = now + timedelta(seconds=data.ttl_seconds)
        lock.released_at = None
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Resource was concurrently locked") from exc
    audit(session, auth, "lock.acquired", resource_type="lock", resource_id=str(lock.id))
    if commit:
        session.commit()
        session.refresh(lock)
    return lock, token


def renew_lock(
    session: Session,
    auth: AuthContext,
    lock_id: UUID,
    *,
    token: str,
    fence: int,
    ttl_seconds: int,
) -> ResourceLock:
    auth.require("locks.acquire")
    statement = select(ResourceLock).where(
        ResourceLock.id == lock_id, ResourceLock.organization_id == auth.organization_id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    lock = session.scalar(statement)
    now = utcnow()
    if (
        lock is None
        or lock.owner_principal_id != auth.principal_id
        or lock.fence != fence
        or lock.released_at is not None
        or _as_utc(lock.expires_at) <= now
        or not verify_lock_token(token, lock.token_hash)
    ):
        raise HTTPException(status_code=409, detail="Lock lease is invalid or stale")
    lock.expires_at = now + timedelta(seconds=ttl_seconds)
    session.commit()
    session.refresh(lock)
    return lock


def release_lock(
    session: Session, auth: AuthContext, lock_id: UUID, *, token: str, fence: int
) -> None:
    auth.require("locks.acquire")
    statement = select(ResourceLock).where(
        ResourceLock.id == lock_id, ResourceLock.organization_id == auth.organization_id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    lock = session.scalar(statement)
    if (
        lock is None
        or lock.owner_principal_id != auth.principal_id
        or lock.fence != fence
        or lock.released_at is not None
        or not verify_lock_token(token, lock.token_hash)
    ):
        raise HTTPException(status_code=409, detail="Lock lease is invalid or stale")
    lock.released_at = utcnow()
    audit(session, auth, "lock.released", resource_type="lock", resource_id=str(lock.id))
    session.commit()


def enrich_memory(session: Session, memory_id: UUID) -> None:
    memory = session.get(Memory, memory_id, with_for_update=True)
    if memory is None or memory.status != "active":
        return
    result = _classify_traced(f"{memory.subject}\n{memory.content}")
    index_memory_chunks(session, memory)
    if memory.memory_type == "fact" and result.memory_type != "fact":
        memory.memory_type = result.memory_type
    original_sensitivity = memory.sensitivity
    memory.sensitivity = _highest_sensitivity(memory.sensitivity, result.sensitivity)
    memory.metadata_ = {
        **(memory.metadata_ or {}),
        "classification": {
            "provider": get_settings().llm_provider,
            "confidence": result.confidence,
            "suggested_sensitivity": result.sensitivity,
            "original_sensitivity": original_sensitivity,
        },
    }
    now = utcnow()
    with span("write_edges") as edges_span:
        closed = 0
        for existing in session.scalars(
            select(Relationship).where(
                Relationship.organization_id == memory.organization_id,
                Relationship.source_memory_id == memory.id,
            )
        ):
            if (existing.metadata_ or {}).get("extracted") and (
                existing.valid_until is None or _as_utc(existing.valid_until) > now
            ):
                existing.valid_until = now
                closed += 1
        written = 0
        for relationship in result.relationships:
            _edge, created = add_relationship(
                session,
                organization_id=memory.organization_id,
                workspace_id=memory.workspace_id,
                extracted=relationship,
                source_memory_id=memory.id,
                sensitivity=memory.sensitivity,
                valid_from=memory.valid_from,
                valid_until=memory.valid_until,
                metadata={"extracted": True},
            )
            written += int(created)
        edges_span.set(edges_closed=closed, edges_written=written)


def enrich_episode(session: Session, episode_id: UUID) -> None:
    episode = session.get(Episode, episode_id)
    if episode is None or episode.deleted_at is not None or episode.enrichment_status == "complete":
        return
    result = _classify_traced(episode.content)
    memory = Memory(
        episode_id=episode.id,
        organization_id=episode.organization_id,
        workspace_id=episode.workspace_id,
        project_ref=episode.project_ref,
        task_ref=episode.task_ref,
        run_ref=episode.run_ref,
        owner_principal_id=episode.owner_principal_id,
        subject=episode.content.strip().splitlines()[0][:500],
        content=episode.content,
        memory_type=result.memory_type,
        sensitivity=_highest_sensitivity(episode.sensitivity, result.sensitivity),
        confidence=result.confidence,
        valid_from=episode.occurred_at,
        metadata_={"derived_from_episode": True},
    )
    session.add(memory)
    session.flush()
    index_memory_chunks(session, memory)
    with span("write_edges") as edges_span:
        written = 0
        for relationship in result.relationships:
            _edge, created = add_relationship(
                session,
                organization_id=episode.organization_id,
                workspace_id=episode.workspace_id,
                extracted=relationship,
                source_memory_id=memory.id,
                sensitivity=memory.sensitivity,
                valid_from=episode.occurred_at,
                metadata={"extracted": True, "episode_id": str(episode.id)},
            )
            written += int(created)
        edges_span.set(edges_closed=0, edges_written=written, memory_id=str(memory.id))
    episode.enrichment_status = "complete"
    episode.enrichment_error = None

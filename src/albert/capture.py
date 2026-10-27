from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from albert.db import get_session
from albert.models import (
    CaptureCheckpoint,
    CaptureEvent,
    CaptureSession,
    Episode,
    Workspace,
    utcnow,
)
from albert.schemas import (
    CAPTURE_CONTRACT_VERSION,
    CaptureCheckpointRead,
    CaptureCheckpointWrite,
    CaptureEventBatch,
    CaptureEventBatchRead,
    CaptureEventRead,
    CaptureEventWrite,
    CaptureSessionEnd,
    CaptureSessionRead,
    CaptureSessionStart,
)
from albert.security import AuthContext, authenticate
from albert.services import audit, enqueue, reject_secrets

router = APIRouter(prefix="/v1/capture", tags=["capture"])
PRIVILEGED_EVENT_CLASSES = frozenset({"system", "instruction"})


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _require_version(version: str) -> None:
    if version != CAPTURE_CONTRACT_VERSION:
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail={
                "error": "unsupported_capture_contract_version",
                "supported_versions": [CAPTURE_CONTRACT_VERSION],
            },
        )


def _require_event_capability(auth: AuthContext, event_class: str) -> None:
    auth.require("capture.write")
    if event_class in PRIVILEGED_EVENT_CLASSES:
        auth.require("capture.system")


def _workspace(session: Session, auth: AuthContext, requested: UUID | None) -> UUID | None:
    workspace_id = requested if requested is not None else auth.workspace_id
    if auth.workspace_id is not None and workspace_id != auth.workspace_id:
        raise HTTPException(status_code=404, detail="Capture scope not found")
    if workspace_id is not None:
        found = session.scalar(
            select(Workspace.id).where(
                Workspace.id == workspace_id,
                Workspace.organization_id == auth.organization_id,
            )
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Capture scope not found")
    return workspace_id


def _capture_session(
    session: Session, auth: AuthContext, session_id: UUID, *, lock: bool = False
) -> CaptureSession:
    statement = select(CaptureSession).where(
        CaptureSession.id == session_id,
        CaptureSession.organization_id == auth.organization_id,
        CaptureSession.owner_principal_id == auth.principal_id,
        CaptureSession.deleted_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    capture_session = session.scalar(statement)
    if capture_session is None:
        raise HTTPException(status_code=404, detail="Capture session not found")
    return capture_session


def _session_read(value: CaptureSession) -> CaptureSessionRead:
    return CaptureSessionRead(
        id=value.id,
        version=value.contract_version,
        client_session_id=value.client_session_id,
        workspace_id=value.workspace_id,
        status=value.status,
        last_sequence=value.last_sequence,
        started_at=_as_utc(value.started_at),
        ended_at=_as_utc(value.ended_at),
    )


def _event_read(value: CaptureEvent) -> CaptureEventRead:
    if value.episode_id is None:
        raise HTTPException(status_code=409, detail="Capture event episode was deleted")
    return CaptureEventRead(
        id=value.id,
        session_id=value.session_id,
        version=value.contract_version,
        sequence=value.sequence,
        event_class=value.event_class,
        occurred_at=_as_utc(value.occurred_at),
        episode_id=value.episode_id,
        derived_memory_id=value.derived_memory_id,
    )


def _checkpoint_read(value: CaptureCheckpoint) -> CaptureCheckpointRead:
    if value.episode_id is None:
        raise HTTPException(status_code=409, detail="Capture checkpoint episode was deleted")
    return CaptureCheckpointRead(
        id=value.id,
        session_id=value.session_id,
        version=value.contract_version,
        through_sequence=value.through_sequence,
        episode_id=value.episode_id,
        derived_memory_id=value.derived_memory_id,
    )


def _event_hash(data: CaptureEventWrite) -> str:
    return _hash(data.model_dump(mode="json"))


def _existing_event(
    session: Session, capture_session: CaptureSession, data: CaptureEventWrite
) -> CaptureEvent | None:
    existing = session.scalar(
        select(CaptureEvent).where(
            CaptureEvent.session_id == capture_session.id,
            CaptureEvent.idempotency_key == data.idempotency_key,
        )
    )
    if existing is not None and (
        existing.request_hash != _event_hash(data) or existing.sequence != data.sequence
    ):
        raise HTTPException(status_code=409, detail="Idempotency key was reused with new input")
    return existing


def _stage_event(
    session: Session,
    auth: AuthContext,
    capture_session: CaptureSession,
    data: CaptureEventWrite,
) -> CaptureEvent:
    event_id = uuid4()
    occurred_at = data.occurred_at or utcnow()
    content_hash = hashlib.sha256(data.content.encode()).hexdigest()
    episode = Episode(
        organization_id=auth.organization_id,
        workspace_id=capture_session.workspace_id,
        owner_principal_id=auth.principal_id,
        source_type="automatic_capture",
        source_uri=f"capture://{capture_session.id}/events/{data.sequence}",
        content=data.content,
        content_hash=content_hash,
        occurred_at=occurred_at,
        extra={
            "capture_session_id": str(capture_session.id),
            "capture_event_id": str(event_id),
            "capture_event_class": data.event_class,
            **data.metadata,
        },
    )
    session.add(episode)
    session.flush()
    job = enqueue(session, auth.organization_id, "enrich_episode", {"episode_id": str(episode.id)})
    session.flush()
    episode.enrichment_job_id = job.id
    event = CaptureEvent(
        id=event_id,
        session_id=capture_session.id,
        organization_id=auth.organization_id,
        workspace_id=capture_session.workspace_id,
        owner_principal_id=auth.principal_id,
        sequence=data.sequence,
        idempotency_key=data.idempotency_key,
        request_hash=_event_hash(data),
        contract_version=data.version,
        event_class=data.event_class,
        content=data.content,
        metadata_=data.metadata,
        occurred_at=occurred_at,
        episode_id=episode.id,
    )
    session.add(event)
    return event


@router.post("/sessions", response_model=CaptureSessionRead, status_code=201)
def start_session(
    data: CaptureSessionStart,
    response: Response,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureSessionRead:
    auth.require("capture.write")
    _require_version(data.version)
    reject_secrets(data.metadata, what="Capture metadata")
    workspace_id = _workspace(session, auth, data.workspace_id)
    request_hash = _hash(data.model_dump(mode="json"))
    existing = session.scalar(
        select(CaptureSession).where(
            CaptureSession.owner_principal_id == auth.principal_id,
            CaptureSession.idempotency_key == data.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="Idempotency key was reused with new input")
        response.status_code = status.HTTP_200_OK
        return _session_read(existing)
    capture_session = CaptureSession(
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        owner_principal_id=auth.principal_id,
        client_session_id=data.client_session_id,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        contract_version=data.version,
        metadata_=data.metadata,
    )
    session.add(capture_session)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(CaptureSession).where(
                CaptureSession.owner_principal_id == auth.principal_id,
                CaptureSession.idempotency_key == data.idempotency_key,
            )
        )
        if concurrent is not None and concurrent.request_hash == request_hash:
            response.status_code = status.HTTP_200_OK
            return _session_read(concurrent)
        raise HTTPException(status_code=409, detail="Capture session already exists") from exc
    audit(
        session,
        auth,
        "capture.session.started",
        resource_type="capture_session",
        resource_id=str(capture_session.id),
    )
    session.commit()
    return _session_read(capture_session)


@router.post("/sessions/{session_id}/events", response_model=CaptureEventRead, status_code=201)
def write_event(
    session_id: UUID,
    data: CaptureEventWrite,
    response: Response,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureEventRead:
    _require_version(data.version)
    _require_event_capability(auth, data.event_class)
    reject_secrets(data.content, data.metadata, what="Capture event")
    capture_session = _capture_session(session, auth, session_id, lock=True)
    existing = _existing_event(session, capture_session, data)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return _event_read(existing)
    if capture_session.status != "active":
        raise HTTPException(status_code=409, detail="Capture session has ended")
    expected = capture_session.last_sequence + 1
    if data.sequence != expected:
        raise HTTPException(
            status_code=409,
            detail={"error": "out_of_order_event", "expected_sequence": expected},
        )
    try:
        event = _stage_event(session, auth, capture_session, data)
        capture_session.last_sequence = data.sequence
        audit(
            session,
            auth,
            "capture.event.created",
            resource_type="capture_event",
            resource_id=str(event.id),
            detail={"sequence": data.sequence, "event_class": data.event_class},
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(CaptureEvent).where(
                CaptureEvent.session_id == session_id,
                CaptureEvent.idempotency_key == data.idempotency_key,
            )
        )
        if concurrent is not None and concurrent.request_hash == _event_hash(data):
            response.status_code = status.HTTP_200_OK
            return _event_read(concurrent)
        raise HTTPException(status_code=409, detail="Event was concurrently ingested") from exc
    return _event_read(event)


@router.post(
    "/sessions/{session_id}/events/batch", response_model=CaptureEventBatchRead, status_code=201
)
def write_event_batch(
    session_id: UUID,
    data: CaptureEventBatch,
    response: Response,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureEventBatchRead:
    _require_version(data.version)
    for item in data.events:
        _require_version(item.version)
        _require_event_capability(auth, item.event_class)
        reject_secrets(item.content, item.metadata, what="Capture event")
    capture_session = _capture_session(session, auth, session_id, lock=True)
    existing = [_existing_event(session, capture_session, item) for item in data.events]
    if all(item is not None for item in existing):
        response.status_code = status.HTTP_200_OK
        return CaptureEventBatchRead(events=[_event_read(item) for item in existing if item])
    if any(item is not None for item in existing):
        raise HTTPException(status_code=409, detail="Batch overlaps previously stored events")
    if capture_session.status != "active":
        raise HTTPException(status_code=409, detail="Capture session has ended")
    expected = list(
        range(
            capture_session.last_sequence + 1,
            capture_session.last_sequence + len(data.events) + 1,
        )
    )
    actual = [item.sequence for item in data.events]
    if actual != expected:
        raise HTTPException(
            status_code=409,
            detail={"error": "out_of_order_batch", "expected_sequences": expected},
        )
    try:
        events = [_stage_event(session, auth, capture_session, item) for item in data.events]
        capture_session.last_sequence = actual[-1]
        audit(
            session,
            auth,
            "capture.events.created",
            resource_type="capture_session",
            resource_id=str(capture_session.id),
            detail={
                "first_sequence": actual[0],
                "last_sequence": actual[-1],
                "count": len(actual),
            },
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalars(
            select(CaptureEvent).where(
                CaptureEvent.session_id == session_id,
                CaptureEvent.idempotency_key.in_([item.idempotency_key for item in data.events]),
            )
        ).all()
        by_key = {item.idempotency_key: item for item in concurrent}
        if len(by_key) == len(data.events) and all(
            by_key[item.idempotency_key].request_hash == _event_hash(item)
            for item in data.events
        ):
            response.status_code = status.HTTP_200_OK
            return CaptureEventBatchRead(
                events=[_event_read(by_key[item.idempotency_key]) for item in data.events]
            )
        raise HTTPException(status_code=409, detail="Batch was concurrently ingested") from exc
    return CaptureEventBatchRead(events=[_event_read(item) for item in events])


@router.post(
    "/sessions/{session_id}/checkpoints", response_model=CaptureCheckpointRead, status_code=201
)
def write_checkpoint(
    session_id: UUID,
    data: CaptureCheckpointWrite,
    response: Response,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureCheckpointRead:
    auth.require("capture.write")
    _require_version(data.version)
    reject_secrets(data.metadata, what="Capture checkpoint metadata")
    capture_session = _capture_session(session, auth, session_id, lock=True)
    request_hash = _hash(data.model_dump(mode="json"))
    existing = session.scalar(
        select(CaptureCheckpoint).where(
            CaptureCheckpoint.session_id == capture_session.id,
            CaptureCheckpoint.idempotency_key == data.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="Idempotency key was reused with new input")
        response.status_code = status.HTTP_200_OK
        return _checkpoint_read(existing)
    if capture_session.status != "active":
        raise HTTPException(status_code=409, detail="Capture session has ended")
    if data.through_sequence > capture_session.last_sequence:
        raise HTTPException(status_code=409, detail="Checkpoint exceeds the stored event sequence")
    event = session.scalar(
        select(CaptureEvent).where(
            CaptureEvent.session_id == capture_session.id,
            CaptureEvent.sequence == data.through_sequence,
            CaptureEvent.deleted_at.is_(None),
        )
    )
    if event is None or event.episode_id is None:
        raise HTTPException(status_code=409, detail="Checkpoint event is unavailable")
    checkpoint = CaptureCheckpoint(
        session_id=capture_session.id,
        organization_id=auth.organization_id,
        workspace_id=capture_session.workspace_id,
        owner_principal_id=auth.principal_id,
        through_sequence=data.through_sequence,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        contract_version=data.version,
        episode_id=event.episode_id,
        derived_memory_id=event.derived_memory_id,
        metadata_=data.metadata,
    )
    session.add(checkpoint)
    try:
        audit(
            session,
            auth,
            "capture.checkpoint.created",
            resource_type="capture_checkpoint",
            resource_id=str(checkpoint.id),
            detail={"through_sequence": data.through_sequence},
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(CaptureCheckpoint).where(
                CaptureCheckpoint.session_id == session_id,
                CaptureCheckpoint.idempotency_key == data.idempotency_key,
            )
        )
        if concurrent is not None and concurrent.request_hash == request_hash:
            response.status_code = status.HTTP_200_OK
            return _checkpoint_read(concurrent)
        raise HTTPException(status_code=409, detail="Checkpoint was concurrently written") from exc
    return _checkpoint_read(checkpoint)


@router.post("/sessions/{session_id}/end", response_model=CaptureSessionRead)
def end_session(
    session_id: UUID,
    data: CaptureSessionEnd,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureSessionRead:
    auth.require("capture.write")
    _require_version(data.version)
    capture_session = _capture_session(session, auth, session_id, lock=True)
    if capture_session.status == "ended":
        if (
            capture_session.end_idempotency_key == data.idempotency_key
            and capture_session.last_sequence == data.final_sequence
        ):
            return _session_read(capture_session)
        raise HTTPException(status_code=409, detail="Capture session has already ended")
    if data.final_sequence != capture_session.last_sequence:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "final_sequence_mismatch",
                "stored_sequence": capture_session.last_sequence,
            },
        )
    capture_session.status = "ended"
    capture_session.ended_at = utcnow()
    capture_session.end_idempotency_key = data.idempotency_key
    audit(
        session,
        auth,
        "capture.session.ended",
        resource_type="capture_session",
        resource_id=str(capture_session.id),
        detail={"final_sequence": data.final_sequence},
    )
    session.commit()
    return _session_read(capture_session)

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from albert.capture_security import screen_capture
from albert.db import get_session
from albert.models import (
    CaptureCheckpoint,
    CaptureEvent,
    CaptureProfile,
    CaptureSession,
    Episode,
    Workspace,
    utcnow,
)
from albert.provenance import merge_event_provenance, normalize_provenance
from albert.schemas import (
    CAPTURE_CONTRACT_VERSION,
    CaptureCheckpointRead,
    CaptureCheckpointWrite,
    CaptureEventBatch,
    CaptureEventBatchRead,
    CaptureEventRead,
    CaptureEventWrite,
    CaptureProfileRead,
    CaptureProfileWrite,
    CaptureSessionEnd,
    CaptureSessionRead,
    CaptureSessionStart,
)
from albert.security import AuthContext, authenticate
from albert.services import audit, enqueue

router = APIRouter(prefix="/v1/capture", tags=["capture"])
PRIVILEGED_EVENT_CLASSES = frozenset({"system", "instruction"})
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")
DEFAULT_EVENT_CLASSES = frozenset(
    {
        "session_started",
        "user_goal",
        "assistant_outcome",
        "selected_tool_outcome",
        "decision",
        "correction",
        "vcs_checkpoint",
        "pre_compaction_checkpoint",
        "session_ended",
        "failure_summary",
    }
)


@dataclass(frozen=True)
class CapturePolicy:
    profile_key: str
    version: int
    allowed_event_classes: frozenset[str]
    metadata_allowlists: dict[str, list[str]]
    event_bounds: dict[str, dict[str, int]]
    sensitivity_floor: str
    source_trust: str
    retention_days: int
    raw_body_policy: str
    secret_policy: str
    derivative_policy: str
    deny_reasons: dict[str, str]


@dataclass(frozen=True)
class PolicyResult:
    data: CaptureEventWrite
    policy: CapturePolicy
    dropped_fields: tuple[str, ...]
    request_hash: str
    screening_decision: str
    screening_reasons: tuple[str, ...]


def _default_policy() -> CapturePolicy:
    return CapturePolicy(
        profile_key="safe-default",
        version=1,
        allowed_event_classes=DEFAULT_EVENT_CLASSES,
        metadata_allowlists={event_class: [] for event_class in DEFAULT_EVENT_CLASSES},
        event_bounds={
            event_class: {"max_content_chars": 64_000, "max_metadata_chars": 64_000}
            for event_class in DEFAULT_EVENT_CLASSES
        },
        sensitivity_floor="internal",
        source_trust="untrusted",
        retention_days=30,
        raw_body_policy="deny",
        secret_policy="reject",
        derivative_policy="cascade",
        deny_reasons={},
    )


def _profile_policy(value: CaptureProfile) -> CapturePolicy:
    return CapturePolicy(
        profile_key=value.profile_key,
        version=value.version,
        allowed_event_classes=frozenset(value.allowed_event_classes),
        metadata_allowlists=value.metadata_allowlists,
        event_bounds=value.event_bounds,
        sensitivity_floor=value.sensitivity_floor,
        source_trust=value.source_trust,
        retention_days=value.retention_days,
        raw_body_policy=value.raw_body_policy,
        secret_policy=value.secret_policy,
        derivative_policy=value.derivative_policy,
        deny_reasons=value.deny_reasons,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _reject_secret_identifiers(*values: str) -> None:
    screening = screen_capture("", {"values": list(values)})
    if screening.reasons:
        raise HTTPException(
            status_code=422,
            detail={"error": "capture_identifier_rejected", "reasons": list(screening.reasons)},
        )


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


def _latest_policy(
    session: Session, auth: AuthContext, workspace_id: UUID | None, profile_key: str
) -> CapturePolicy:
    if profile_key == "safe-default":
        return _default_policy()
    profile = session.scalar(
        select(CaptureProfile)
        .where(
            CaptureProfile.organization_id == auth.organization_id,
            CaptureProfile.workspace_id == workspace_id,
            CaptureProfile.profile_key == profile_key,
        )
        .order_by(CaptureProfile.version.desc())
        .limit(1)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Capture profile not found")
    return _profile_policy(profile)


def _deny_policy(
    session: Session,
    auth: AuthContext,
    capture_session: CaptureSession,
    policy: CapturePolicy,
    event_class: str,
    reason: str,
) -> None:
    public_reason = policy.deny_reasons.get(event_class, policy.deny_reasons.get(reason, reason))
    audit(
        session,
        auth,
        "capture.policy.denied",
        resource_type="capture_session",
        resource_id=str(capture_session.id),
        detail={
            "event_class": event_class,
            "profile_key": policy.profile_key,
            "profile_version": policy.version,
            "decision": "deny",
            "reason": public_reason,
        },
    )
    session.commit()
    raise HTTPException(
        status_code=403,
        detail={"error": "capture_policy_denied", "reason": public_reason},
    )


def _apply_policy(
    session: Session,
    auth: AuthContext,
    capture_session: CaptureSession,
    data: CaptureEventWrite,
) -> PolicyResult:
    policy = _latest_policy(
        session, auth, capture_session.workspace_id, capture_session.profile_key
    )
    allowed_classes = policy.allowed_event_classes
    if capture_session.local_event_classes is not None:
        allowed_classes &= frozenset(capture_session.local_event_classes)
    if data.event_class not in allowed_classes:
        _deny_policy(
            session, auth, capture_session, policy, data.event_class, "event_class_not_allowed"
        )
    if data.body_kind == "raw" and policy.raw_body_policy != "allow":
        _deny_policy(session, auth, capture_session, policy, data.event_class, "raw_body_denied")

    screening = screen_capture(data.content, data.metadata)
    if screening.reasons and policy.secret_policy == "reject":
        audit(
            session,
            auth,
            "capture.secret.rejected",
            resource_type="capture_session",
            resource_id=str(capture_session.id),
            detail={
                "event_class": data.event_class,
                "profile_key": policy.profile_key,
                "profile_version": policy.version,
                "decision": "reject",
                "reasons": list(screening.reasons),
            },
        )
        session.commit()
        raise HTTPException(
            status_code=422,
            detail={"error": "capture_secret_rejected", "reasons": list(screening.reasons)},
        )
    screened_data = data.model_copy(
        update={"content": screening.content, "metadata": screening.metadata}
    )

    bounds = policy.event_bounds.get(data.event_class, {})
    max_content = bounds.get("max_content_chars", 64_000)
    if len(screened_data.content) > max_content:
        _deny_policy(session, auth, capture_session, policy, data.event_class, "content_too_large")

    allowed_metadata = set(policy.metadata_allowlists.get(data.event_class, []))
    if capture_session.local_metadata_allowlists is not None:
        allowed_metadata &= set(capture_session.local_metadata_allowlists.get(data.event_class, []))
    filtered_metadata = {
        key: value for key, value in screened_data.metadata.items() if key in allowed_metadata
    }
    max_metadata = bounds.get("max_metadata_chars", 64_000)
    if len(json.dumps(filtered_metadata, default=str)) > max_metadata:
        _deny_policy(session, auth, capture_session, policy, data.event_class, "metadata_too_large")

    sensitivity = max(
        (screened_data.sensitivity, policy.sensitivity_floor), key=SENSITIVITY_ORDER.index
    )
    if sensitivity not in auth.allowed_sensitivities():
        _deny_policy(
            session, auth, capture_session, policy, data.event_class, "credential_capability_denied"
        )
    dropped = tuple(sorted(set(screened_data.metadata) - set(filtered_metadata)))
    effective = screened_data.model_copy(
        update={"metadata": filtered_metadata, "sensitivity": sensitivity}
    )
    return PolicyResult(
        data=effective,
        policy=policy,
        dropped_fields=dropped,
        request_hash=_event_hash(data),
        screening_decision="redacted" if screening.reasons else "clean",
        screening_reasons=screening.reasons,
    )


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
        profile_key=value.profile_key,
        status=value.status,
        last_sequence=value.last_sequence,
        started_at=_as_utc(value.started_at),
        ended_at=_as_utc(value.ended_at),
        provenance=value.provenance,
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
        profile_key=value.profile_key,
        profile_version=value.profile_version,
        sensitivity=value.sensitivity,
        source_trust=value.source_trust,
        retention_days=value.retention_days,
        screening_decision=value.screening_decision,
        screening_reasons=value.screening_reasons,
        occurred_at=_as_utc(value.occurred_at),
        episode_id=value.episode_id,
        derived_memory_id=value.derived_memory_id,
        provenance=value.provenance,
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
    result: PolicyResult,
) -> CaptureEvent:
    data = result.data
    policy = result.policy
    event_id = uuid4()
    occurred_at = data.occurred_at or utcnow()
    content_hash = hashlib.sha256(data.content.encode()).hexdigest()
    observed = normalize_provenance(
        data.provenance,
        organization_id=auth.organization_id,
        allow_absolute_path="admin" in auth.capabilities,
    )
    provenance = merge_event_provenance(capture_session.provenance, observed)
    project_identity = provenance.get("project_id")
    source_commit = provenance.get("source_commit")
    branch_ref = provenance.get("branch") or provenance.get("ref")
    episode = Episode(
        organization_id=auth.organization_id,
        workspace_id=capture_session.workspace_id,
        project_ref=str(project_identity) if project_identity else None,
        task_ref=str(provenance["task"]) if provenance.get("task") else None,
        run_ref=str(provenance["run"]) if provenance.get("run") else None,
        owner_principal_id=auth.principal_id,
        source_type="automatic_capture",
        source_uri=f"capture://{capture_session.id}/events/{data.sequence}",
        content=data.content,
        content_hash=content_hash,
        sensitivity=data.sensitivity,
        occurred_at=occurred_at,
        extra={
            **data.metadata,
            "capture_session_id": str(capture_session.id),
            "capture_event_id": str(event_id),
            "capture_event_class": data.event_class,
            "capture_profile_key": policy.profile_key,
            "capture_profile_version": policy.version,
            "capture_source_trust": policy.source_trust,
            "capture_retention_days": policy.retention_days,
            "provenance": provenance,
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
        request_hash=result.request_hash,
        contract_version=data.version,
        event_class=data.event_class,
        profile_key=policy.profile_key,
        profile_version=policy.version,
        policy_decision="allow",
        sensitivity=data.sensitivity,
        source_trust=policy.source_trust,
        retention_days=policy.retention_days,
        body_kind=data.body_kind,
        screening_decision=result.screening_decision,
        screening_reasons=list(result.screening_reasons),
        derivative_policy=policy.derivative_policy,
        content=data.content,
        metadata_=data.metadata,
        provenance=provenance,
        project_identity=str(project_identity) if project_identity else None,
        source_commit=str(source_commit) if source_commit else None,
        branch_ref=str(branch_ref) if branch_ref else None,
        occurred_at=occurred_at,
        episode_id=episode.id,
    )
    session.add(event)
    return event


def _profile_read(value: CaptureProfile) -> CaptureProfileRead:
    return CaptureProfileRead(
        id=value.id,
        profile_key=value.profile_key,
        version=value.version,
        workspace_id=value.workspace_id,
        allowed_event_classes=value.allowed_event_classes,
        metadata_allowlists=value.metadata_allowlists,
        event_bounds=value.event_bounds,
        sensitivity_floor=value.sensitivity_floor,
        source_trust=value.source_trust,
        retention_days=value.retention_days,
        raw_body_policy=value.raw_body_policy,
        secret_policy=value.secret_policy,
        derivative_policy=value.derivative_policy,
        deny_reasons=value.deny_reasons,
        created_at=_as_utc(value.created_at),
    )


@router.post("/profiles", response_model=CaptureProfileRead, status_code=201)
def create_profile(
    data: CaptureProfileWrite,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureProfileRead:
    auth.require("capture.system")
    if data.profile_key == "safe-default":
        raise HTTPException(status_code=422, detail="safe-default is reserved")
    workspace_id = _workspace(session, auth, data.workspace_id)
    latest = session.scalar(
        select(CaptureProfile)
        .where(
            CaptureProfile.organization_id == auth.organization_id,
            CaptureProfile.workspace_id == workspace_id,
            CaptureProfile.profile_key == data.profile_key,
        )
        .order_by(CaptureProfile.version.desc())
        .limit(1)
    )
    expected_version = 1 if latest is None else latest.version + 1
    if data.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "capture_profile_version_conflict",
                "expected_version": expected_version,
            },
        )
    profile = CaptureProfile(
        organization_id=auth.organization_id,
        workspace_id=workspace_id,
        scope_key=str(workspace_id) if workspace_id is not None else "organization",
        profile_key=data.profile_key,
        version=data.version,
        allowed_event_classes=data.allowed_event_classes,
        metadata_allowlists=data.metadata_allowlists,
        event_bounds=data.event_bounds,
        sensitivity_floor=data.sensitivity_floor,
        source_trust=data.source_trust,
        retention_days=data.retention_days,
        raw_body_policy=data.raw_body_policy,
        secret_policy=data.secret_policy,
        derivative_policy=data.derivative_policy,
        deny_reasons=data.deny_reasons,
    )
    session.add(profile)
    try:
        session.flush()
        audit(
            session,
            auth,
            "capture.profile.created",
            resource_type="capture_profile",
            resource_id=str(profile.id),
            detail={"profile_key": data.profile_key, "profile_version": data.version},
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Capture profile version already exists"
        ) from exc
    session.refresh(profile)
    return _profile_read(profile)


@router.get("/profiles/{profile_key}", response_model=CaptureProfileRead)
def get_profile(
    profile_key: str,
    workspace_id: UUID | None = None,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureProfileRead:
    auth.require("capture.write")
    resolved_workspace = _workspace(session, auth, workspace_id)
    profile = session.scalar(
        select(CaptureProfile)
        .where(
            CaptureProfile.organization_id == auth.organization_id,
            CaptureProfile.workspace_id == resolved_workspace,
            CaptureProfile.profile_key == profile_key,
        )
        .order_by(CaptureProfile.version.desc())
        .limit(1)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Capture profile not found")
    return _profile_read(profile)


@router.post("/sessions", response_model=CaptureSessionRead, status_code=201)
def start_session(
    data: CaptureSessionStart,
    response: Response,
    auth: AuthContext = Depends(authenticate),
    session: Session = Depends(get_session),
) -> CaptureSessionRead:
    auth.require("capture.write")
    _require_version(data.version)
    _reject_secret_identifiers(data.client_session_id, data.idempotency_key)
    workspace_id = _workspace(session, auth, data.workspace_id)
    policy = _latest_policy(session, auth, workspace_id, data.profile_key)
    screened_metadata = screen_capture("", data.metadata)
    if screened_metadata.reasons and policy.secret_policy == "reject":
        raise HTTPException(
            status_code=422,
            detail={
                "error": "capture_secret_rejected",
                "reasons": list(screened_metadata.reasons),
            },
        )
    if data.local_event_classes is not None:
        local_classes = set(data.local_event_classes)
        if len(local_classes) != len(data.local_event_classes):
            raise HTTPException(status_code=422, detail="Local event classes must be unique")
        if not local_classes.issubset(policy.allowed_event_classes):
            raise HTTPException(status_code=422, detail="Local policy cannot widen Brain policy")
    if data.local_metadata_allowlists is not None:
        local_classes = (
            set(data.local_event_classes)
            if data.local_event_classes is not None
            else policy.allowed_event_classes
        )
        for event_class, fields in data.local_metadata_allowlists.items():
            if event_class not in local_classes or not set(fields).issubset(
                policy.metadata_allowlists.get(event_class, [])
            ):
                raise HTTPException(
                    status_code=422, detail="Local policy cannot widen Brain policy"
                )
    provenance = normalize_provenance(
        data.provenance,
        organization_id=auth.organization_id,
        allow_absolute_path="admin" in auth.capabilities,
    )
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
        profile_key=data.profile_key,
        local_event_classes=data.local_event_classes,
        local_metadata_allowlists=data.local_metadata_allowlists,
        metadata_=screened_metadata.metadata,
        provenance=provenance,
        project_identity=provenance.get("project_id"),
        source_commit=provenance.get("source_commit"),
        branch_ref=provenance.get("branch") or provenance.get("ref"),
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
        detail={"profile_key": policy.profile_key, "profile_version": policy.version},
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
    _reject_secret_identifiers(data.idempotency_key)
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
    result = _apply_policy(session, auth, capture_session, data)
    try:
        event = _stage_event(session, auth, capture_session, result)
        capture_session.last_sequence = data.sequence
        audit(
            session,
            auth,
            "capture.event.created",
            resource_type="capture_event",
            resource_id=str(event.id),
            detail={
                "sequence": data.sequence,
                "event_class": data.event_class,
                "profile_key": result.policy.profile_key,
                "profile_version": result.policy.version,
                "decision": "allow",
                "dropped_fields": list(result.dropped_fields),
                "screening_decision": result.screening_decision,
                "screening_reasons": list(result.screening_reasons),
            },
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
        _reject_secret_identifiers(item.idempotency_key)
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
    results = [_apply_policy(session, auth, capture_session, item) for item in data.events]
    try:
        events = [_stage_event(session, auth, capture_session, item) for item in results]
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
                "profile_key": results[0].policy.profile_key,
                "profile_version": results[0].policy.version,
                "decision": "allow",
                "dropped_field_count": sum(len(item.dropped_fields) for item in results),
                "redacted_count": sum(item.screening_decision == "redacted" for item in results),
                "screening_reasons": sorted(
                    {reason for item in results for reason in item.screening_reasons}
                ),
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
            by_key[item.idempotency_key].request_hash == _event_hash(item) for item in data.events
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
    _reject_secret_identifiers(data.idempotency_key)
    capture_session = _capture_session(session, auth, session_id, lock=True)
    policy = _latest_policy(
        session, auth, capture_session.workspace_id, capture_session.profile_key
    )
    screened_metadata = screen_capture("", data.metadata)
    if screened_metadata.reasons and policy.secret_policy == "reject":
        raise HTTPException(
            status_code=422,
            detail={
                "error": "capture_secret_rejected",
                "reasons": list(screened_metadata.reasons),
            },
        )
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
        metadata_=screened_metadata.metadata,
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
    _reject_secret_identifiers(data.idempotency_key)
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

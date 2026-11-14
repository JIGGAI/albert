"""Deterministic, evidence-backed standing profile materialization."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.models import Memory, Principal, ProfileAssertion, ProfileMaterialization, utcnow
from albert.recorder import span
from albert.schemas import ProfileMaterializationStatus, StandingProfileRead
from albert.scope import covers, in_workspace
from albert.security import AuthContext
from albert.services import audit

TRUST_ORDER = ("untrusted", "verified", "trusted")
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _workspace_scope(workspace_id: UUID | None) -> str:
    return str(workspace_id) if workspace_id else "organization"


def _target(
    session: Session,
    auth: AuthContext,
    principal_id: UUID,
    capability: str,
) -> Principal:
    auth.require(capability)
    principal = session.scalar(
        select(Principal).where(
            Principal.id == principal_id,
            Principal.organization_id == auth.organization_id,
            Principal.active.is_(True),
        )
    )
    organization_capability = f"{capability}.organization"
    if (
        principal is None
        or not covers(auth.workspace_id, principal.workspace_id)
        or (
            principal.id != auth.principal_id
            and "admin" not in auth.capabilities
            and organization_capability not in auth.capabilities
        )
    ):
        # Do not disclose whether a foreign or under-scoped principal exists.
        raise HTTPException(status_code=404, detail="Profile not found")
    return principal


def _materialization(session: Session, principal: Principal) -> ProfileMaterialization | None:
    return session.scalar(
        select(ProfileMaterialization).where(
            ProfileMaterialization.organization_id == principal.organization_id,
            ProfileMaterialization.workspace_scope == _workspace_scope(principal.workspace_id),
            ProfileMaterialization.principal_id == principal.id,
        )
    )


def _canonical_memories(session: Session, principal: Principal) -> list[Memory]:
    now = datetime.now(UTC)
    records = session.scalars(
        select(Memory).where(
            Memory.organization_id == principal.organization_id,
            in_workspace(Memory.workspace_id, principal.workspace_id),
            Memory.owner_principal_id == principal.id,
            Memory.status == "active",
        )
    ).all()
    return [
        memory
        for memory in records
        if _as_utc(memory.valid_from) <= now
        and (memory.valid_until is None or _as_utc(memory.valid_until) > now)
    ]


def _view_digest(schema_version: str, rows: list[ProfileAssertion]) -> str:
    """Digest only visible assertions, preventing sensitivity side channels."""
    payload = {
        "schema_version": schema_version,
        "assertions": [
            {
                "bucket": row.bucket,
                "value": row.value,
                "valid_from": _as_utc(row.valid_from).isoformat(),
                "valid_until": _as_utc(row.valid_until).isoformat() if row.valid_until else None,
                "trust": row.trust,
                "confidence": row.confidence,
                "sensitivity": row.sensitivity,
                "citations": row.citations,
            }
            for row in rows
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _derive(records: list[Memory]) -> tuple[list[dict[str, object]], str]:
    settings = get_settings()
    bucket_for_type = {
        memory_type: bucket
        for bucket, memory_types in settings.profile_buckets.items()
        for memory_type in memory_types
    }
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    source_rows: list[dict[str, object]] = []
    for memory in records:
        bucket = bucket_for_type.get(memory.memory_type.lower())
        if bucket is None:
            continue
        value = " ".join(memory.content.split())
        if not value:
            continue
        metadata = memory.metadata_ if isinstance(memory.metadata_, dict) else {}
        trust = str(metadata.get("source_trust", "untrusted")).lower()
        if trust not in TRUST_ORDER:
            trust = "untrusted"
        source_rows.append(
            {
                "id": str(memory.id),
                "bucket": bucket,
                "value": value,
                "valid_from": _as_utc(memory.valid_from).isoformat(),
                "valid_until": _as_utc(memory.valid_until).isoformat()
                if memory.valid_until
                else None,
                "trust": trust,
                "confidence": memory.confidence,
                "sensitivity": memory.sensitivity,
                "updated_at": _as_utc(memory.updated_at).isoformat(),
            }
        )
        key = (bucket, value.casefold())
        assertion = grouped.get(key)
        if assertion is None:
            grouped[key] = {
                "bucket": bucket,
                "value": value,
                "valid_from": memory.valid_from,
                "valid_until": memory.valid_until,
                "trust": trust,
                "confidence": memory.confidence,
                "sensitivity": memory.sensitivity,
                "citations": [str(memory.id)],
            }
            continue
        assertion["valid_from"] = min(assertion["valid_from"], memory.valid_from)  # type: ignore[type-var]
        previous_until = assertion["valid_until"]
        assertion["valid_until"] = (
            None
            if previous_until is None or memory.valid_until is None
            else max(previous_until, memory.valid_until)  # type: ignore[type-var]
        )
        assertion["trust"] = max(
            (str(assertion["trust"]), trust), key=TRUST_ORDER.index
        )
        assertion["confidence"] = max(float(assertion["confidence"]), memory.confidence)
        assertion["sensitivity"] = max(
            (str(assertion["sensitivity"]), memory.sensitivity), key=SENSITIVITY_ORDER.index
        )
        assertion["citations"] = sorted([*assertion["citations"], str(memory.id)])  # type: ignore[misc]

    bucket_order = {name: index for index, name in enumerate(settings.profile_buckets)}
    assertions = sorted(
        grouped.values(),
        key=lambda item: (bucket_order[str(item["bucket"])], str(item["value"]).casefold()),
    )
    digest_payload = {
        "schema_version": settings.profile_schema_version,
        "buckets": settings.profile_buckets,
        "sources": sorted(source_rows, key=lambda row: str(row["id"])),
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return assertions, digest


def rebuild_profile(
    session: Session, auth: AuthContext, principal_id: UUID
) -> StandingProfileRead:
    principal = _target(session, auth, principal_id, "profile.rebuild")
    settings = get_settings()
    with span("profile.materialize", principal_id=str(principal.id)) as handle:
        assertions, digest = _derive(_canonical_memories(session, principal))
        materialization = _materialization(session, principal)
        if materialization is None:
            materialization = ProfileMaterialization(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                workspace_scope=_workspace_scope(principal.workspace_id),
                principal_id=principal.id,
                schema_version=settings.profile_schema_version,
                bucket_config=settings.profile_buckets,
                source_digest=digest,
            )
            session.add(materialization)
            session.flush()
        else:
            session.execute(
                delete(ProfileAssertion).where(
                    ProfileAssertion.materialization_id == materialization.id
                )
            )
            session.flush()
        for ordinal, item in enumerate(assertions):
            session.add(
                ProfileAssertion(
                    materialization_id=materialization.id,
                    organization_id=principal.organization_id,
                    workspace_id=principal.workspace_id,
                    principal_id=principal.id,
                    ordinal=ordinal,
                    **item,
                )
            )
        materialization.schema_version = settings.profile_schema_version
        materialization.bucket_config = settings.profile_buckets
        materialization.source_digest = digest
        materialization.assertion_count = len(assertions)
        materialization.built_at = utcnow()
        audit(
            session,
            auth,
            "profile.rebuilt",
            resource_type="profile",
            resource_id=str(principal.id),
            detail={"source_digest": digest, "assertion_count": len(assertions)},
        )
        session.commit()
        handle.set(source_digest=digest, assertion_count=len(assertions))
    return _render_profile(session, auth, principal, settings.profile_max_assertions)


def read_profile(
    session: Session, auth: AuthContext, principal_id: UUID, budget: int
) -> StandingProfileRead:
    principal = _target(session, auth, principal_id, "profile.read")
    return _render_profile(session, auth, principal, budget)


def _render_profile(
    session: Session, auth: AuthContext, principal: Principal, budget: int
) -> StandingProfileRead:
    materialization = _materialization(session, principal)
    if materialization is None:
        raise HTTPException(status_code=404, detail="Profile not materialized")
    allowed = auth.allowed_sensitivities()
    rows = session.scalars(
        select(ProfileAssertion)
        .where(
            ProfileAssertion.materialization_id == materialization.id,
            ProfileAssertion.sensitivity.in_(allowed),
        )
        .order_by(ProfileAssertion.ordinal)
    ).all()
    effective_budget = min(budget, get_settings().profile_max_assertions)
    selected = rows[:effective_budget]
    with span("profile.select", principal_id=str(principal.id)) as handle:
        handle.set(
            candidate_count=len(rows),
            selected_count=len(selected),
            truncated=len(rows) > effective_budget,
        )
    return StandingProfileRead(
        principal_id=principal.id,
        workspace_id=principal.workspace_id,
        schema_version=materialization.schema_version,
        source_digest=_view_digest(materialization.schema_version, rows),
        built_at=materialization.built_at,
        assertions=[
            {
                "bucket": row.bucket,
                "value": row.value,
                "valid_from": row.valid_from,
                "valid_until": row.valid_until,
                "trust": row.trust,
                "confidence": row.confidence,
                "sensitivity": row.sensitivity,
                "citations": row.citations,
            }
            for row in selected
        ],
        total=len(rows),
        truncated=len(rows) > effective_budget,
    )


def profile_status(
    session: Session, auth: AuthContext, principal_id: UUID
) -> ProfileMaterializationStatus:
    principal = _target(session, auth, principal_id, "profile.read")
    materialization = _materialization(session, principal)
    if materialization is None:
        return ProfileMaterializationStatus(
            principal_id=principal.id, workspace_id=principal.workspace_id, state="never"
        )
    rows = session.scalars(
        select(ProfileAssertion)
        .where(
            ProfileAssertion.materialization_id == materialization.id,
            ProfileAssertion.sensitivity.in_(auth.allowed_sensitivities()),
        )
        .order_by(ProfileAssertion.ordinal)
    ).all()
    return ProfileMaterializationStatus(
        principal_id=principal.id,
        workspace_id=principal.workspace_id,
        state="current",
        schema_version=materialization.schema_version,
        source_digest=_view_digest(materialization.schema_version, rows),
        assertion_count=len(rows),
        built_at=materialization.built_at,
    )

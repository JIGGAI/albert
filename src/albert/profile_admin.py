"""Organization-authorized administration contracts for standing profiles."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from albert.models import (
    Principal,
    ProfileAssertion,
    ProfileAssertionHistory,
    ProfileBucketPolicy,
    ProfileMaterialization,
)
from albert.schemas import (
    ProfileAssertionAdminRead,
    ProfileAssertionHistoryPage,
    ProfileAssertionHistoryRead,
    ProfileAssertionMutation,
    ProfileAssertionPage,
    ProfileBucketPolicyRead,
    ProfileBucketPolicyWrite,
    ProfilePrincipalPage,
    ProfilePrincipalSummary,
)
from albert.scope import covers, in_workspace
from albert.security import AuthContext
from albert.services import audit


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _scope(auth: AuthContext) -> str:
    return str(auth.workspace_id) if auth.workspace_id else "organization"


def _authorize(auth: AuthContext) -> None:
    auth.require("profile.admin")


def _target(session: Session, auth: AuthContext, principal_id: UUID) -> Principal:
    _authorize(auth)
    principal = session.scalar(
        select(Principal).where(
            Principal.id == principal_id,
            Principal.organization_id == auth.organization_id,
            Principal.active.is_(True),
        )
    )
    if principal is None or not covers(auth.workspace_id, principal.workspace_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return principal


def read_policy(session: Session, auth: AuthContext) -> ProfileBucketPolicyRead:
    _authorize(auth)
    row = session.scalar(
        select(ProfileBucketPolicy)
        .where(
            ProfileBucketPolicy.organization_id == auth.organization_id,
            ProfileBucketPolicy.workspace_scope == _scope(auth),
        )
        .order_by(ProfileBucketPolicy.version.desc())
        .limit(1)
    )
    if row is None:
        return ProfileBucketPolicyRead(version=0, buckets={})
    return ProfileBucketPolicyRead.model_validate(row, from_attributes=True)


def write_policy(
    session: Session, auth: AuthContext, data: ProfileBucketPolicyWrite
) -> ProfileBucketPolicyRead:
    _authorize(auth)
    digest = _digest({"expected_version": data.expected_version, "buckets": data.buckets})
    retry = session.scalar(
        select(ProfileBucketPolicy).where(
            ProfileBucketPolicy.organization_id == auth.organization_id,
            ProfileBucketPolicy.workspace_scope == _scope(auth),
            ProfileBucketPolicy.idempotency_key == data.idempotency_key,
        )
    )
    if retry is not None:
        if retry.request_digest != digest:
            raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
        return ProfileBucketPolicyRead.model_validate(retry, from_attributes=True)
    current = read_policy(session, auth)
    if current.version != data.expected_version:
        raise HTTPException(status_code=409, detail="Profile policy version conflict")
    row = ProfileBucketPolicy(
        organization_id=auth.organization_id,
        workspace_id=auth.workspace_id,
        workspace_scope=_scope(auth),
        version=current.version + 1,
        buckets=data.buckets,
        idempotency_key=data.idempotency_key,
        request_digest=digest,
        actor_principal_id=auth.principal_id,
    )
    session.add(row)
    audit(
        session,
        auth,
        "profile.policy_updated",
        resource_type="profile_policy",
        resource_id=_scope(auth),
        detail={
            "version": row.version,
            "prior_value": current.buckets,
            "new_value": data.buckets,
            "request_identity": data.idempotency_key,
        },
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(ProfileBucketPolicy).where(
                ProfileBucketPolicy.organization_id == auth.organization_id,
                ProfileBucketPolicy.workspace_scope == _scope(auth),
                ProfileBucketPolicy.idempotency_key == data.idempotency_key,
            )
        )
        if concurrent is not None and concurrent.request_digest == digest:
            return ProfileBucketPolicyRead.model_validate(concurrent, from_attributes=True)
        raise HTTPException(status_code=409, detail="Profile policy version conflict") from exc
    session.refresh(row)
    return ProfileBucketPolicyRead.model_validate(row, from_attributes=True)


def list_principals(
    session: Session, auth: AuthContext, offset: int, limit: int
) -> ProfilePrincipalPage:
    _authorize(auth)
    conditions = [
        Principal.organization_id == auth.organization_id,
        Principal.active.is_(True),
        in_workspace(Principal.workspace_id, auth.workspace_id),
    ]
    total = session.scalar(select(func.count()).select_from(Principal).where(*conditions)) or 0
    rows = session.execute(
        select(Principal, ProfileMaterialization)
        .outerjoin(
            ProfileMaterialization,
            ProfileMaterialization.principal_id == Principal.id,
        )
        .where(*conditions)
        .order_by(Principal.name, Principal.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return ProfilePrincipalPage(
        items=[
            ProfilePrincipalSummary(
                principal_id=p.id,
                workspace_id=p.workspace_id,
                name=p.name,
                principal_type=p.principal_type,
                materialized=m is not None,
                assertion_count=(
                    session.scalar(
                        select(func.count())
                        .select_from(ProfileAssertion)
                        .where(
                            ProfileAssertion.principal_id == p.id,
                            ProfileAssertion.sensitivity.in_(auth.allowed_sensitivities()),
                            ProfileAssertion.suppressed.is_(False),
                        )
                    )
                    or 0
                ),
                built_at=m.built_at if m else None,
            )
            for p, m in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


def _assertion_read(row: ProfileAssertion) -> ProfileAssertionAdminRead:
    return ProfileAssertionAdminRead.model_validate(row, from_attributes=True)


def list_assertions(
    session: Session, auth: AuthContext, principal_id: UUID, offset: int, limit: int
) -> ProfileAssertionPage:
    _target(session, auth, principal_id)
    conditions = [
        ProfileAssertion.organization_id == auth.organization_id,
        ProfileAssertion.principal_id == principal_id,
        ProfileAssertion.sensitivity.in_(auth.allowed_sensitivities()),
    ]
    total = (
        session.scalar(select(func.count()).select_from(ProfileAssertion).where(*conditions)) or 0
    )
    rows = session.scalars(
        select(ProfileAssertion)
        .where(*conditions)
        .order_by(ProfileAssertion.bucket, ProfileAssertion.ordinal, ProfileAssertion.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return ProfileAssertionPage(
        items=[_assertion_read(row) for row in rows], total=total, offset=offset, limit=limit
    )


def mutate_assertion(
    session: Session,
    auth: AuthContext,
    assertion_id: UUID,
    data: ProfileAssertionMutation,
    action: str,
) -> ProfileAssertionAdminRead:
    _authorize(auth)
    digest = _digest(
        {
            "assertion_id": assertion_id,
            "action": action,
            "value": data.value,
            "expected_version": data.expected_version,
        }
    )
    retry = session.scalar(
        select(ProfileAssertionHistory).where(
            ProfileAssertionHistory.organization_id == auth.organization_id,
            ProfileAssertionHistory.idempotency_key == data.idempotency_key,
        )
    )
    if retry is not None:
        if retry.request_digest != digest:
            raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
        row = session.get(ProfileAssertion, retry.assertion_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Profile assertion not found")
        return _assertion_read(row)
    row = session.scalar(
        select(ProfileAssertion)
        .where(
            ProfileAssertion.id == assertion_id,
            ProfileAssertion.organization_id == auth.organization_id,
            in_workspace(ProfileAssertion.workspace_id, auth.workspace_id),
            ProfileAssertion.sensitivity.in_(auth.allowed_sensitivities()),
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Profile assertion not found")
    if row.version != data.expected_version:
        raise HTTPException(status_code=409, detail="Profile assertion version conflict")
    if action == "correct" and data.value is None:
        raise HTTPException(status_code=422, detail="Correction requires a value")
    if data.value is not None and not data.value.strip():
        raise HTTPException(status_code=422, detail="Correction requires a visible value")
    prior = row.value
    row.version += 1
    row.decision_state = "corrected" if action == "correct" else "suppressed"
    row.suppressed = action == "suppress"
    if data.value is not None:
        row.value = " ".join(data.value.split())
    key = row.assertion_key or _digest({"bucket": row.bucket, "citations": row.citations})
    row.assertion_key = key
    session.add(
        ProfileAssertionHistory(
            organization_id=row.organization_id,
            workspace_id=row.workspace_id,
            principal_id=row.principal_id,
            assertion_id=row.id,
            assertion_key=key,
            action=action,
            version=row.version,
            prior_value=prior,
            new_value=row.value,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            trust=row.trust,
            confidence=row.confidence,
            sensitivity=row.sensitivity,
            decision_state=row.decision_state,
            citations=row.citations,
            actor_principal_id=auth.principal_id,
            idempotency_key=data.idempotency_key,
            request_digest=digest,
        )
    )
    audit(
        session,
        auth,
        f"profile.assertion_{action}ed",
        resource_type="profile_assertion",
        resource_id=str(row.id),
        detail={
            "version": row.version,
            "citation_count": len(row.citations),
            "request_identity": data.idempotency_key,
        },
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(ProfileAssertionHistory).where(
                ProfileAssertionHistory.organization_id == auth.organization_id,
                ProfileAssertionHistory.idempotency_key == data.idempotency_key,
            )
        )
        if concurrent is not None and concurrent.request_digest == digest:
            current = session.get(ProfileAssertion, concurrent.assertion_id)
            if current is not None:
                return _assertion_read(current)
        raise HTTPException(status_code=409, detail="Profile assertion version conflict") from exc
    session.refresh(row)
    return _assertion_read(row)


def list_history(
    session: Session, auth: AuthContext, principal_id: UUID, offset: int, limit: int
) -> ProfileAssertionHistoryPage:
    _target(session, auth, principal_id)
    conditions = [
        ProfileAssertionHistory.organization_id == auth.organization_id,
        ProfileAssertionHistory.principal_id == principal_id,
        in_workspace(ProfileAssertionHistory.workspace_id, auth.workspace_id),
        ProfileAssertionHistory.sensitivity.in_(auth.allowed_sensitivities()),
    ]
    total = (
        session.scalar(select(func.count()).select_from(ProfileAssertionHistory).where(*conditions))
        or 0
    )
    rows = session.scalars(
        select(ProfileAssertionHistory)
        .where(*conditions)
        .order_by(ProfileAssertionHistory.created_at, ProfileAssertionHistory.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return ProfileAssertionHistoryPage(
        items=[
            ProfileAssertionHistoryRead.model_validate(row, from_attributes=True) for row in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )

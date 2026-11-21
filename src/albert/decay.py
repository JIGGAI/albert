"""Versioned, deterministic memory decay without canonical evidence deletion."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from albert.models import DecayPolicy, DecayState, Memory
from albert.recorder import span
from albert.schemas import (
    DecayPolicyRead,
    DecayPolicyRollback,
    DecayPolicyWrite,
    DecayRecompute,
    DecayRunRead,
    DecayStateRead,
)
from albert.scope import in_workspace
from albert.security import AuthContext
from albert.services import audit

SECONDS_PER_DAY = 86_400
MINIMUM_MULTIPLIER = 0.01


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _scope(workspace_id: UUID | None) -> str:
    return str(workspace_id) if workspace_id else "organization"


def _policy_query(auth: AuthContext):  # type: ignore[no-untyped-def]
    return (
        DecayPolicy.organization_id == auth.organization_id,
        DecayPolicy.workspace_scope == _scope(auth.workspace_id),
    )


def _policy_read(policy: DecayPolicy) -> DecayPolicyRead:
    return DecayPolicyRead(
        id=policy.id,
        workspace_id=policy.workspace_id,
        version=policy.version,
        rules=policy.rules,
        active=policy.active,
        rollback_of_version=policy.rollback_of_version,
        created_by_principal_id=policy.created_by_principal_id,
        created_at=policy.created_at,
    )


def get_decay_policy(session: Session, auth: AuthContext) -> DecayPolicyRead:
    auth.require("memory.read")
    policy = session.scalar(
        select(DecayPolicy).where(*_policy_query(auth), DecayPolicy.active.is_(True))
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="Decay policy not configured")
    return _policy_read(policy)


def _create_policy(
    session: Session,
    auth: AuthContext,
    rules: dict[str, object],
    *,
    expected_version: int | None,
    rollback_of_version: int | None = None,
) -> DecayPolicyRead:
    auth.require("admin")
    current = session.scalar(
        select(DecayPolicy).where(*_policy_query(auth), DecayPolicy.active.is_(True))
    )
    current_version = current.version if current else 0
    if expected_version is not None and expected_version != current_version:
        raise HTTPException(status_code=409, detail="Decay policy version changed")
    if current is not None:
        current.active = False
    policy = DecayPolicy(
        organization_id=auth.organization_id,
        workspace_id=auth.workspace_id,
        workspace_scope=_scope(auth.workspace_id),
        version=current_version + 1,
        rules=rules,
        active=True,
        created_by_principal_id=auth.principal_id,
        rollback_of_version=rollback_of_version,
    )
    session.add(policy)
    session.flush()
    audit(
        session,
        auth,
        "decay.policy_changed",
        resource_type="decay_policy",
        resource_id=str(policy.id),
        detail={
            "version": policy.version,
            "prior_version": current_version or None,
            "rollback_of_version": rollback_of_version,
            "classes": sorted(rules),
        },
    )
    session.commit()
    session.refresh(policy)
    return _policy_read(policy)


def set_decay_policy(
    session: Session, auth: AuthContext, data: DecayPolicyWrite
) -> DecayPolicyRead:
    rules = {name: rule.model_dump(mode="json") for name, rule in data.rules.items()}
    if "default" not in rules:
        raise HTTPException(status_code=422, detail="Decay policy requires a default rule")
    return _create_policy(session, auth, rules, expected_version=data.expected_version)


def rollback_decay_policy(
    session: Session, auth: AuthContext, data: DecayPolicyRollback
) -> DecayPolicyRead:
    auth.require("admin")
    target = session.scalar(
        select(DecayPolicy).where(*_policy_query(auth), DecayPolicy.version == data.version)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Decay policy version not found")
    current = session.scalar(
        select(DecayPolicy).where(*_policy_query(auth), DecayPolicy.active.is_(True))
    )
    return _create_policy(
        session,
        auth,
        target.rules,
        expected_version=current.version if current else 0,
        rollback_of_version=target.version,
    )


def _freshness(memory: Memory) -> datetime:
    raw = (memory.metadata_ or {}).get("freshness_at")
    if raw:
        try:
            return _as_utc(datetime.fromisoformat(str(raw)))
        except ValueError:
            pass
    return _as_utc(memory.valid_from)


def _protection(memory: Memory, rule: dict[str, object]) -> str | None:
    metadata = memory.metadata_ or {}
    if bool(rule.get("protected")):
        return "protected_class"
    for key in ("pinned", "legal_hold", "active_correction"):
        if metadata.get(key):
            return key
    if memory.memory_type == "correction":
        return "active_correction"
    return None


def _effective(
    memory: Memory, rule: dict[str, object], evaluated_at: datetime
) -> tuple[datetime, float, float, bool, bool, str]:
    freshness_at = _freshness(memory)
    age_days = max(0.0, (evaluated_at - freshness_at).total_seconds() / SECONDS_PER_DAY)
    protection = _protection(memory, rule)
    if protection:
        return freshness_at, age_days, 1.0, False, True, protection
    fresh_days = float(rule["freshness_days"])
    if age_days <= fresh_days:
        return freshness_at, age_days, 1.0, False, False, "fresh"
    elapsed = age_days - fresh_days
    stale_state = str(rule.get("stale_state", "demote"))
    multiplier = (
        max(MINIMUM_MULTIPLIER, 0.5 ** (elapsed / float(rule["half_life_days"])))
        if stale_state == "demote"
        else 1.0
    )
    reason = (
        "retention_elapsed" if age_days >= float(rule["retention_days"]) else "freshness_elapsed"
    )
    return freshness_at, age_days, multiplier, True, False, reason


def _state_read(state: DecayState) -> DecayStateRead:
    return DecayStateRead.model_validate(state, from_attributes=True)


def recompute_decay(session: Session, auth: AuthContext, data: DecayRecompute) -> DecayRunRead:
    auth.require("admin")
    query = select(DecayPolicy).where(*_policy_query(auth))
    query = query.where(
        DecayPolicy.version == data.policy_version
        if data.policy_version is not None
        else DecayPolicy.active.is_(True)
    )
    policy = session.scalar(query)
    if policy is None:
        raise HTTPException(status_code=404, detail="Decay policy not found")
    evaluated_at = _as_utc(data.as_of or datetime.now(UTC))
    memories = list(
        session.scalars(
            select(Memory).where(
                Memory.organization_id == auth.organization_id,
                in_workspace(Memory.workspace_id, auth.workspace_id),
                Memory.status == "active",
            )
        )
    )
    existing = {
        state.memory_id: state
        for state in session.scalars(
            select(DecayState).where(
                DecayState.organization_id == auth.organization_id,
                in_workspace(DecayState.workspace_id, auth.workspace_id),
            )
        )
    }
    states: list[DecayState] = []
    changed_count = 0
    with span(
        "decay.recompute", policy_version=policy.version, evaluated_at=evaluated_at.isoformat()
    ) as handle:
        for memory in memories:
            rule_class = memory.memory_type if memory.memory_type in policy.rules else "default"
            rule = policy.rules[rule_class]
            freshness, age, multiplier, stale, protected, reason = _effective(
                memory, rule, evaluated_at
            )
            rounded_age = round(age, 6)
            rounded_multiplier = round(multiplier, 8)
            state = existing.get(memory.id)
            changed = state is None or (
                state.policy_id != policy.id
                or state.policy_version != policy.version
                or state.rule_class != rule_class
                or _as_utc(state.freshness_at) != freshness
                or _as_utc(state.evaluated_at) != evaluated_at
                or state.age_days != rounded_age
                or state.score_multiplier != rounded_multiplier
                or state.stale != stale
                or state.protected != protected
                or state.reason != reason
            )
            if state is None:
                state = DecayState(
                    memory_id=memory.id,
                    organization_id=memory.organization_id,
                    workspace_id=memory.workspace_id,
                )
                session.add(state)
            state.policy_id = policy.id
            state.policy_version = policy.version
            state.rule_class = rule_class
            state.freshness_at = freshness
            state.evaluated_at = evaluated_at
            state.age_days = rounded_age
            state.score_multiplier = rounded_multiplier
            state.stale = stale
            state.protected = protected
            state.reason = reason
            states.append(state)
            changed_count += int(changed)
        session.flush()
        stale_count = sum(state.stale for state in states)
        protected_count = sum(state.protected for state in states)
        if changed_count:
            audit(
                session,
                auth,
                "decay.recomputed",
                resource_type="decay_policy",
                resource_id=str(policy.id),
                detail={
                    "policy_version": policy.version,
                    "evaluated": len(states),
                    "changed": changed_count,
                    "stale": stale_count,
                    "protected": protected_count,
                    "evaluated_at": evaluated_at.isoformat(),
                },
            )
        session.commit()
        handle.set(
            evaluated=len(states),
            changed=changed_count,
            stale=stale_count,
            protected=protected_count,
        )
    return DecayRunRead(
        policy_version=policy.version,
        evaluated_at=evaluated_at,
        evaluated=len(states),
        stale=stale_count,
        protected=protected_count,
        states=[_state_read(state) for state in sorted(states, key=lambda row: str(row.memory_id))],
    )


def read_decay_state(session: Session, auth: AuthContext, memory_id: UUID) -> DecayStateRead:
    auth.require("memory.read")
    state = session.scalar(
        select(DecayState).where(
            DecayState.memory_id == memory_id,
            DecayState.organization_id == auth.organization_id,
            in_workspace(DecayState.workspace_id, auth.workspace_id),
        )
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Decay state not found")
    return _state_read(state)

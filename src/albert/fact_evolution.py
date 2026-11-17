"""Deterministic, evidence-cited standing-fact evolution."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from albert.memory_safety import CONFIDENCE_CEILINGS, trust_level
from albert.models import FactEvolution, Memory, utcnow
from albert.recorder import span
from albert.schemas import (
    FactEvolutionCreate,
    FactEvolutionHistory,
    FactEvolutionRead,
    FactEvolutionReview,
)
from albert.scope import in_workspace
from albert.security import AuthContext
from albert.services import audit, enqueue

TRUST_ORDER = ("untrusted", "verified", "trusted")
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")
RELATION_DECISIONS = {
    "same": "extend",
    "adds": "extend",
    "supersedes": "update",
    "conflicts": "contradict",
    "ambiguous": "review",
}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _request_digest(data: FactEvolutionCreate) -> str:
    payload = data.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _memory(
    session: Session,
    auth: AuthContext,
    memory_id: UUID,
    *,
    active: bool = True,
    owner_principal_id: UUID | None = None,
) -> Memory:
    conditions = [
        Memory.id == memory_id,
        Memory.organization_id == auth.organization_id,
        Memory.owner_principal_id == (owner_principal_id or auth.principal_id),
        in_workspace(Memory.workspace_id, auth.workspace_id),
    ]
    if active:
        conditions.append(Memory.status == "active")
    record = session.scalar(select(Memory).where(*conditions))
    if record is None or record.sensitivity not in auth.allowed_sensitivities():
        raise HTTPException(status_code=404, detail="Memory not found")
    return record


def _classify(
    data: FactEvolutionCreate, evidence: Memory, fact: Memory | None
) -> tuple[str, str, str | None]:
    if data.relation in {"same", "supersedes", "conflicts"} and fact is None:
        raise HTTPException(status_code=422, detail="This relation requires a standing fact")
    decision = (
        "derive"
        if data.relation == "adds" and fact is None
        else RELATION_DECISIONS[data.relation]
    )
    if data.relation == "ambiguous":
        return decision, "ambiguous_relation", "ambiguous_relation"
    metadata = evidence.metadata_ or {}
    if metadata.get("quarantined") or metadata.get("suspicious_instruction"):
        return decision, "poisoning_control", "evidence_quarantined"
    if data.impact == "high":
        return decision, "high_impact_policy", "high_impact_policy"
    if fact is not None and decision in {"update", "contradict"}:
        evidence_trust = trust_level(metadata)
        fact_trust = trust_level(fact.metadata_ or {})
        if TRUST_ORDER.index(evidence_trust) < TRUST_ORDER.index(fact_trust):
            return decision, "lower_trust_replacement", "lower_trust_evidence"
    rationale = {
        "derive": "new_supported_fact",
        "extend": "compatible_support_or_extension",
        "update": "newer_superseding_evidence",
        "contradict": "conflicting_evidence",
    }[decision]
    return decision, rationale, None


def _apply(
    session: Session,
    evolution: FactEvolution,
    evidence: Memory,
    fact: Memory | None,
) -> None:
    effective_from = _as_utc(evolution.effective_from)
    if fact is not None and evolution.decision in {"update", "contradict"}:
        fact.status = "superseded"
        if effective_from > _as_utc(fact.valid_from):
            fact.valid_until = effective_from

    if evolution.decision == "extend" and evolution.relation == "same" and fact is not None:
        fact.status = "superseded"
        if effective_from > _as_utc(fact.valid_from):
            fact.valid_until = effective_from
        content = fact.content
        subject = fact.subject
        memory_type = fact.memory_type
    else:
        content = evidence.content
        subject = evidence.subject
        memory_type = "fact"

    # An additive extension preserves the existing fact and adds the cited assertion.
    sensitivity = max(
        [evidence.sensitivity, fact.sensitivity if fact is not None else evidence.sensitivity],
        key=SENSITIVITY_ORDER.index,
    )
    metadata = dict(evidence.metadata_ or {})
    metadata.update(
        {
            "capture_source_trust": evolution.source_trust,
            "source_trust": evolution.source_trust,
            "confidence_ceiling": evolution.confidence_ceiling,
            "evolution_id": str(evolution.id),
            "evolution_decision": evolution.decision,
            "evolution_citations": evolution.citations,
            "quarantined": False,
            "quarantine_reviewed": evolution.reviewed_at is not None,
        }
    )
    result = Memory(
        organization_id=evolution.organization_id,
        workspace_id=evolution.workspace_id,
        project_ref=evidence.project_ref,
        task_ref=evidence.task_ref,
        run_ref=evidence.run_ref,
        owner_principal_id=evolution.principal_id,
        subject=subject,
        content=content,
        memory_type=memory_type,
        sensitivity=sensitivity,
        confidence=evolution.effective_confidence,
        valid_from=evolution.effective_from,
        valid_until=evolution.effective_until,
        metadata_=metadata,
    )
    session.add(result)
    session.flush()
    evolution.result_memory_id = result.id
    evolution.state = "applied"
    enqueue(
        session,
        evolution.organization_id,
        "enrich_memory",
        {"memory_id": str(result.id)},
    )


def evolve_fact(
    session: Session, auth: AuthContext, data: FactEvolutionCreate
) -> FactEvolutionRead:
    auth.require("memory.write")
    digest = _request_digest(data)
    existing = session.scalar(
        select(FactEvolution).where(
            FactEvolution.organization_id == auth.organization_id,
            FactEvolution.principal_id == auth.principal_id,
            FactEvolution.idempotency_key == data.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_digest != digest:
            raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
        return _read(existing)

    evidence = _memory(session, auth, data.evidence_memory_id)
    fact = _memory(session, auth, data.fact_memory_id) if data.fact_memory_id else None
    if fact is not None and fact.id == evidence.id:
        raise HTTPException(status_code=422, detail="Evidence and standing fact must differ")
    decision, rationale, review_reason = _classify(data, evidence, fact)
    source_trust = trust_level(evidence.metadata_ or {})
    ceiling = float(
        (evidence.metadata_ or {}).get("confidence_ceiling", CONFIDENCE_CEILINGS[source_trust])
    )
    confidence = min(evidence.confidence, ceiling)
    effective_from = data.effective_from or evidence.valid_from
    if data.effective_until is not None and _as_utc(data.effective_until) <= _as_utc(
        effective_from
    ):
        raise HTTPException(status_code=422, detail="effective_until must be after effective_from")
    citations = sorted(
        [str(evidence.id), *([str(fact.id)] if fact is not None else [])]
    )
    evolution = FactEvolution(
        organization_id=auth.organization_id,
        workspace_id=evidence.workspace_id,
        principal_id=auth.principal_id,
        idempotency_key=data.idempotency_key,
        request_digest=digest,
        evidence_memory_id=evidence.id,
        fact_memory_id=fact.id if fact is not None else None,
        relation=data.relation,
        decision=decision,
        state="pending_review" if review_reason else "applied",
        rationale_code=rationale,
        review_reason=review_reason,
        impact=data.impact,
        source_trust=source_trust,
        confidence_ceiling=ceiling,
        effective_confidence=confidence,
        effective_from=effective_from,
        effective_until=data.effective_until,
        citations=citations,
    )
    session.add(evolution)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(FactEvolution).where(
                FactEvolution.organization_id == auth.organization_id,
                FactEvolution.principal_id == auth.principal_id,
                FactEvolution.idempotency_key == data.idempotency_key,
            )
        )
        if concurrent is not None and concurrent.request_digest == digest:
            return _read(concurrent)
        raise HTTPException(status_code=409, detail="Idempotency key payload mismatch") from exc
    with span("fact.evolve", decision=decision, review_required=bool(review_reason)) as handle:
        if review_reason is None:
            _apply(session, evolution, evidence, fact)
        audit(
            session,
            auth,
            "fact.evolution_classified",
            resource_type="fact_evolution",
            resource_id=str(evolution.id),
            detail={
                "decision": decision,
                "state": evolution.state,
                "rationale_code": rationale,
                "citation_count": len(citations),
            },
        )
        session.commit()
        handle.set(state=evolution.state, citation_count=len(citations))
    session.refresh(evolution)
    return _read(evolution)


def review_fact_evolution(
    session: Session,
    auth: AuthContext,
    evolution_id: UUID,
    data: FactEvolutionReview,
) -> FactEvolutionRead:
    auth.require("memory.review")
    evolution = _scoped_evolution(session, auth, evolution_id, owner_only=False)
    if evolution.state != "pending_review":
        expected = "rejected" if data.action == "reject" else "applied"
        if evolution.state == expected:
            return _read(evolution)
        raise HTTPException(status_code=409, detail="Evolution review is already complete")
    if evolution.relation == "ambiguous":
        if data.action == "approve" and data.relation is None:
            raise HTTPException(status_code=422, detail="Approval must resolve the relation")
        if data.relation is not None:
            evolution.relation = data.relation
            evolution.decision = RELATION_DECISIONS[data.relation]
            if data.relation == "adds" and evolution.fact_memory_id is None:
                evolution.decision = "derive"
            if (
                data.relation in {"same", "supersedes", "conflicts"}
                and evolution.fact_memory_id is None
            ):
                raise HTTPException(
                    status_code=422, detail="This relation requires a standing fact"
                )
            evolution.rationale_code = "reviewer_resolved_relation"

    evolution.reviewed_by_principal_id = auth.principal_id
    evolution.reviewed_at = utcnow()
    if data.action == "reject":
        evolution.state = "rejected"
        evolution.rationale_code = "reviewer_rejected"
    else:
        evidence = _memory(
            session,
            auth,
            evolution.evidence_memory_id,  # type: ignore[arg-type]
            owner_principal_id=evolution.principal_id,
        )
        fact = (
            _memory(
                session,
                auth,
                evolution.fact_memory_id,
                owner_principal_id=evolution.principal_id,
            )
            if evolution.fact_memory_id is not None
            else None
        )
        _apply(session, evolution, evidence, fact)
        evolution.rationale_code = "reviewer_approved"
    audit(
        session,
        auth,
        f"fact.evolution_{data.action}d",
        resource_type="fact_evolution",
        resource_id=str(evolution.id),
        detail={"decision": evolution.decision, "state": evolution.state},
    )
    session.commit()
    session.refresh(evolution)
    return _read(evolution)


def get_fact_evolution(
    session: Session, auth: AuthContext, evolution_id: UUID
) -> FactEvolutionRead:
    auth.require("memory.read")
    return _read(_scoped_evolution(session, auth, evolution_id))


def rebuild_fact_evolution_history(
    session: Session, auth: AuthContext
) -> FactEvolutionHistory:
    """Rebuild an ordered view/digest without creating decisions or audit rows."""
    auth.require("memory.read")
    rows = list(
        session.scalars(
            select(FactEvolution)
            .where(
                FactEvolution.organization_id == auth.organization_id,
                FactEvolution.principal_id == auth.principal_id,
                in_workspace(FactEvolution.workspace_id, auth.workspace_id),
            )
            .order_by(FactEvolution.created_at, FactEvolution.id)
        )
    )
    payload = [
        {
            "request_digest": row.request_digest,
            "decision": row.decision,
            "state": row.state,
            "rationale_code": row.rationale_code,
            "citations": row.citations,
            "result_memory_id": str(row.result_memory_id) if row.result_memory_id else None,
        }
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FactEvolutionHistory(
        count=len(rows), history_digest=digest, decisions=[_read(row) for row in rows]
    )


def _scoped_evolution(
    session: Session,
    auth: AuthContext,
    evolution_id: UUID,
    *,
    owner_only: bool = True,
) -> FactEvolution:
    conditions = [
        FactEvolution.id == evolution_id,
        FactEvolution.organization_id == auth.organization_id,
        in_workspace(FactEvolution.workspace_id, auth.workspace_id),
    ]
    if owner_only:
        conditions.append(FactEvolution.principal_id == auth.principal_id)
    record = session.scalar(select(FactEvolution).where(*conditions))
    if record is None:
        raise HTTPException(status_code=404, detail="Fact evolution not found")
    return record


def _read(record: FactEvolution) -> FactEvolutionRead:
    return FactEvolutionRead(
        id=record.id,
        evidence_memory_id=record.evidence_memory_id,
        fact_memory_id=record.fact_memory_id,
        result_memory_id=record.result_memory_id,
        relation=record.relation,  # type: ignore[arg-type]
        decision=record.decision,  # type: ignore[arg-type]
        state=record.state,  # type: ignore[arg-type]
        rationale_code=record.rationale_code,
        review_reason=record.review_reason,
        impact=record.impact,  # type: ignore[arg-type]
        source_trust=record.source_trust,  # type: ignore[arg-type]
        confidence_ceiling=record.confidence_ceiling,
        effective_confidence=record.effective_confidence,
        effective_from=record.effective_from,
        effective_until=record.effective_until,
        citations=record.citations,
        reviewed_by_principal_id=record.reviewed_by_principal_id,
        reviewed_at=record.reviewed_at,
        created_at=record.created_at,
    )

from __future__ import annotations

import hashlib
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from albert.models import CaptureCheckpoint, CaptureEvent, Episode, Memory, utcnow

_SENSITIVITY = ("public", "internal", "confidential", "restricted")
_DURABLE_CLASSES = {
    "assistant_outcome",
    "correction",
    "decision",
    "failure_summary",
    "pre_compaction_checkpoint",
    "selected_tool_outcome",
    "vcs_checkpoint",
}
_CHATTER = re.compile(
    r"^(?:ok(?:ay)?|thanks?|hello|hi|got it|sounds good|working on it|done)[.!\s]*$", re.I
)
_PROCEDURE = re.compile(r"\b(?:steps?|procedure|runbook|how to|first.+then)\b", re.I | re.S)
_LESSON = re.compile(r"\b(?:lesson|learned|root cause|failed because|avoid)\b", re.I)


def _normalized(content: str) -> str:
    return " ".join(content.casefold().split())


def _memory_type(event: CaptureEvent) -> str:
    if event.event_class == "decision":
        return "decision"
    if event.event_class == "failure_summary" or _LESSON.search(event.content):
        return "lesson"
    if _PROCEDURE.search(event.content):
        return "procedure"
    if event.event_class in {"vcs_checkpoint", "pre_compaction_checkpoint"}:
        return "project_state"
    return "fact"


def _fingerprint(event: CaptureEvent) -> str:
    material = f"{_memory_type(event)}\0{_normalized(event.content)}"
    return hashlib.sha256(material.encode()).hexdigest()


def _durable(event: CaptureEvent) -> bool:
    content = event.content.strip()
    return bool(
        event.event_class in _DURABLE_CLASSES
        and len(content) >= 12
        and not _CHATTER.match(content)
    )


def _promotion_metadata(
    checkpoint: CaptureCheckpoint, event: CaptureEvent, fingerprint: str
) -> dict[str, object]:
    return {
        "automatic_capture_promotion": True,
        "capture_checkpoint_id": str(checkpoint.id),
        "capture_session_id": str(checkpoint.session_id),
        "capture_event_id": str(event.id),
        "capture_event_sequence": event.sequence,
        "capture_event_class": event.event_class,
        "capture_profile_key": event.profile_key,
        "capture_profile_version": event.profile_version,
        "capture_source_trust": event.source_trust,
        "capture_retention_days": event.retention_days,
        "capture_derivative_policy": event.derivative_policy,
        "capture_promotion_fingerprint": fingerprint,
        "provenance": event.provenance,
    }


def _existing_canonical(session: Session, event: CaptureEvent, fingerprint: str) -> Memory | None:
    candidates = session.scalars(
        select(Memory).where(
            Memory.organization_id == event.organization_id,
            Memory.workspace_id == event.workspace_id,
            Memory.owner_principal_id == event.owner_principal_id,
            Memory.status == "active",
        )
    )
    return next(
        (
            memory
            for memory in candidates
            if (memory.metadata_ or {}).get("capture_promotion_fingerprint") == fingerprint
        ),
        None,
    )


def _event_memory(session: Session, event: CaptureEvent) -> Memory | None:
    if event.derived_memory_id is not None:
        memory = session.get(Memory, event.derived_memory_id)
        if memory is not None:
            return memory
    return session.scalar(
        select(Memory).where(Memory.episode_id == event.episode_id, Memory.status == "active")
    )


def _discard_memory(session: Session, event: CaptureEvent, memory: Memory | None) -> None:
    episode = session.get(Episode, event.episode_id) if event.episode_id else None
    if episode is not None:
        episode.extra = {**episode.extra, "capture_promotion_decision": "discard"}
    if memory is not None and memory.status != "deleted":
        from albert.services import _scrub_memory

        _scrub_memory(session, memory, utcnow())
    event.derived_memory_id = None


def _supersede_previous(session: Session, event: CaptureEvent, memory: Memory) -> None:
    if event.event_class != "correction":
        return
    previous = session.scalar(
        select(Memory)
        .join(CaptureEvent, CaptureEvent.derived_memory_id == Memory.id)
        .where(
            CaptureEvent.session_id == event.session_id,
            CaptureEvent.sequence < event.sequence,
            Memory.status == "active",
            Memory.id != memory.id,
        )
        .order_by(CaptureEvent.sequence.desc())
        .limit(1)
    )
    if previous is None:
        return
    previous.status = "superseded"
    previous.valid_until = event.occurred_at
    previous.metadata_ = {
        **(previous.metadata_ or {}),
        "superseded_by_memory_id": str(memory.id),
        "superseded_at": event.occurred_at.isoformat(),
    }
    memory.metadata_ = {**(memory.metadata_ or {}), "supersedes_memory_id": str(previous.id)}


def promote_capture_checkpoint(session: Session, checkpoint_id: UUID) -> None:
    checkpoint = session.get(CaptureCheckpoint, checkpoint_id)
    if checkpoint is None or checkpoint.deleted_at is not None:
        return
    if checkpoint.promotion_status == "complete":
        return
    checkpoint.promotion_status = "running"
    checkpoint.promotion_error = None
    session.commit()

    checkpoint = session.get(CaptureCheckpoint, checkpoint_id)
    assert checkpoint is not None
    prior_sequence = session.scalar(
        select(CaptureCheckpoint.through_sequence)
        .where(
            CaptureCheckpoint.session_id == checkpoint.session_id,
            CaptureCheckpoint.through_sequence < checkpoint.through_sequence,
            CaptureCheckpoint.deleted_at.is_(None),
        )
        .order_by(CaptureCheckpoint.through_sequence.desc())
        .limit(1)
    ) or 0
    events = list(
        session.scalars(
            select(CaptureEvent)
            .where(
                CaptureEvent.session_id == checkpoint.session_id,
                CaptureEvent.sequence > prior_sequence,
                CaptureEvent.sequence <= checkpoint.through_sequence,
                CaptureEvent.deleted_at.is_(None),
                CaptureEvent.organization_id == checkpoint.organization_id,
                CaptureEvent.workspace_id == checkpoint.workspace_id,
                CaptureEvent.owner_principal_id == checkpoint.owner_principal_id,
            )
            .order_by(CaptureEvent.sequence)
        )
    )
    promoted: list[Memory] = []
    for event in events:
        memory = _event_memory(session, event)
        if not _durable(event):
            _discard_memory(session, event, memory)
            continue
        if memory is None:
            from albert.services import enrich_episode

            enrich_episode(session, event.episode_id)
            memory = _event_memory(session, event)
        if memory is None:
            raise RuntimeError(f"capture event {event.id} has no derived memory")
        fingerprint = _fingerprint(event)
        canonical = _existing_canonical(session, event, fingerprint)
        if canonical is not None and canonical.id != memory.id:
            _discard_memory(session, event, memory)
            event.derived_memory_id = canonical.id
            promoted.append(canonical)
            continue
        memory.memory_type = _memory_type(event)
        memory.sensitivity = max(
            memory.sensitivity, event.sensitivity, key=_SENSITIVITY.index
        )
        memory.valid_from = event.occurred_at
        memory.metadata_ = {
            **(memory.metadata_ or {}),
            **_promotion_metadata(checkpoint, event, fingerprint),
        }
        episode = session.get(Episode, event.episode_id) if event.episode_id else None
        if episode is not None:
            episode.extra = {**episode.extra, "capture_promotion_decision": "promote"}
        event.derived_memory_id = memory.id
        _supersede_previous(session, event, memory)
        promoted.append(memory)

    unique = {memory.id: memory for memory in promoted}
    checkpoint.derived_memory_id = promoted[-1].id if promoted else None
    checkpoint.metadata_ = {
        **checkpoint.metadata_,
        "promoted_memory_ids": [str(memory_id) for memory_id in unique],
        "promoted_count": len(unique),
        "discarded_count": len(events) - len(promoted),
    }
    checkpoint.promotion_status = "complete"
    checkpoint.promotion_error = None
    session.commit()

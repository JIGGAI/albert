from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from albert.models import CaptureCheckpoint, CaptureEvent, Episode, Memory, utcnow


def _expire_event(session: Session, event: CaptureEvent, now: datetime) -> None:
    if event.deleted_at is not None:
        return
    from albert.services import _scrub_memory

    memories = list(
        session.scalars(
            select(Memory).where(Memory.episode_id == event.episode_id, Memory.status != "deleted")
        )
    )
    for memory in memories:
        _scrub_memory(session, memory, now)
        if event.derivative_policy == "invalidate":
            memory.status = "invalidated"
    episode = session.get(Episode, event.episode_id) if event.episode_id is not None else None
    if episode is not None and episode.deleted_at is None:
        episode.content = ""
        episode.extra = {}
        episode.enrichment_error = None
        episode.deleted_at = now
    event.content = ""
    event.metadata_ = {}
    event.deleted_at = now
    session.query(CaptureCheckpoint).filter(
        CaptureCheckpoint.session_id == event.session_id,
        CaptureCheckpoint.through_sequence >= event.sequence,
        CaptureCheckpoint.deleted_at.is_(None),
    ).update({CaptureCheckpoint.deleted_at: now}, synchronize_session=False)


def expire_capture_events(session: Session, *, now: datetime | None = None) -> int:
    """Idempotently expire capture records and provenance-linked derivatives."""
    effective_now = now or utcnow()
    events = list(session.scalars(select(CaptureEvent).where(CaptureEvent.deleted_at.is_(None))))
    expired = 0
    for event in events:
        occurred_at = event.occurred_at
        if occurred_at.tzinfo is None and effective_now.tzinfo is not None:
            occurred_at = occurred_at.replace(tzinfo=effective_now.tzinfo)
        if occurred_at + timedelta(days=event.retention_days) <= effective_now:
            _expire_event(session, event, effective_now)
            expired += 1
    return expired


def delete_capture_for_episode(session: Session, episode_id, now: datetime) -> None:  # type: ignore[no-untyped-def]
    for event in session.scalars(
        select(CaptureEvent).where(
            CaptureEvent.episode_id == episode_id, CaptureEvent.deleted_at.is_(None)
        )
    ):
        event.content = ""
        event.metadata_ = {}
        event.deleted_at = now
        session.query(CaptureCheckpoint).filter(
            CaptureCheckpoint.episode_id == episode_id,
            CaptureCheckpoint.deleted_at.is_(None),
        ).update({CaptureCheckpoint.deleted_at: now}, synchronize_session=False)

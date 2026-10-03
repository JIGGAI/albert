from __future__ import annotations

import logging
import os
import signal
import socket
import time
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.db import SessionLocal
from albert.models import Episode, Job, ResourceLock, WorkingMemory, utcnow
from albert.services import enrich_episode, enrich_memory

logger = logging.getLogger("albert.worker")
_stop = False


def _stop_handler(_signum, _frame) -> None:  # type: ignore[no-untyped-def]
    global _stop
    _stop = True


def claim_job(session: Session, worker_id: str) -> Job | None:
    now = utcnow()
    statement = (
        select(Job)
        .where(Job.status == "pending", Job.available_at <= now)
        .order_by(Job.created_at)
        .limit(1)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    job = session.scalar(statement)
    if job is None:
        return None
    job.status = "running"
    job.locked_at = now
    job.locked_by = worker_id
    job.attempts += 1
    session.commit()
    session.refresh(job)
    return job


def process_job(session: Session, job: Job) -> None:
    try:
        if job.job_type == "enrich_memory":
            enrich_memory(session, UUID(job.payload["memory_id"]))
        elif job.job_type == "enrich_episode":
            enrich_episode(session, UUID(job.payload["episode_id"]))
        else:
            raise ValueError(f"Unknown job type: {job.job_type}")
        job.status = "complete"
        job.last_error = None
        session.commit()
    except Exception as exc:
        session.rollback()
        job = session.get(Job, job.id)
        if job is None:
            raise
        job.last_error = f"{type(exc).__name__}: {exc}"[:10_000]
        if job.attempts >= 5:
            job.status = "failed"
            if job.job_type == "enrich_episode":
                episode = session.get(Episode, UUID(job.payload["episode_id"]))
                if episode is not None:
                    episode.enrichment_status = "failed"
                    episode.enrichment_error = job.last_error
        else:
            job.status = "pending"
            job.available_at = utcnow() + timedelta(seconds=min(300, 2**job.attempts))
        session.commit()
        logger.exception("Job failed", extra={"job_id": str(job.id), "job_type": job.job_type})


def housekeeping(session: Session) -> None:
    now = utcnow()
    expired_ids = list(
        session.scalars(
            select(WorkingMemory.id).where(
                WorkingMemory.status == "active", WorkingMemory.expires_at <= now
            )
        )
    )
    session.query(WorkingMemory).filter(
        WorkingMemory.status == "active", WorkingMemory.expires_at <= now
    ).update({WorkingMemory.status: "expired", WorkingMemory.completed_at: now})
    if expired_ids:
        session.query(ResourceLock).filter(
            ResourceLock.working_memory_id.in_(expired_ids),
            ResourceLock.released_at.is_(None),
        ).update({ResourceLock.released_at: now}, synchronize_session=False)
    session.query(Job).filter(
        Job.status == "running", Job.locked_at < now - timedelta(minutes=15)
    ).update({Job.status: "pending", Job.locked_at: None, Job.locked_by: None})
    session.commit()


def run() -> None:
    global _stop
    logging.basicConfig(level=getattr(logging, get_settings().log_level.upper(), logging.INFO))
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    last_housekeeping = 0.0
    logger.info("Albert worker started", extra={"worker_id": worker_id})
    while not _stop:
        with SessionLocal() as session:
            if time.monotonic() - last_housekeeping > 60:
                housekeeping(session)
                last_housekeeping = time.monotonic()
            job = claim_job(session, worker_id)
            if job is not None:
                process_job(session, job)
                continue
        time.sleep(get_settings().worker_poll_seconds)
    logger.info("Albert worker stopped")


if __name__ == "__main__":
    run()

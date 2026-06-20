from __future__ import annotations

import asyncio
from uuid import UUID

from sqlmodel import Session, select

from app.events import publish_log
from app.models import Artifact, ItemStatus, Job, JobItem, JobStatus, LogEvent, utcnow


def log(session: Session, message: str, level: str = "info", job_id: UUID | None = None) -> LogEvent:
    row = LogEvent(job_id=job_id, level=level, message=message)
    session.add(row)
    session.commit()
    session.refresh(row)
    payload = {
        "type": "log",
        "id": str(row.id),
        "job_id": str(row.job_id) if row.job_id else None,
        "level": row.level,
        "message": row.message,
        "created_at": row.created_at.isoformat(),
    }
    try:
        asyncio.get_running_loop().create_task(publish_log(payload))
    except RuntimeError as exc:
        import logging
        logging.getLogger(__name__).warning("Could not publish log to event stream (no running loop): %s", exc)
    return row


def recompute_job(session: Session, job_id: UUID) -> Job:
    session.expire_all()
    job = session.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    items = session.exec(select(JobItem).where(JobItem.job_id == job_id)).all()
    job.total = len(items)
    job.done = len([i for i in items if i.status == ItemStatus.completed])
    job.failed = len([i for i in items if i.status == ItemStatus.failed])
    if job.status == JobStatus.cancelled:
        pass
    elif items and all(i.status in {ItemStatus.completed, ItemStatus.failed, ItemStatus.cancelled} for i in items):
        if all(i.status == ItemStatus.cancelled for i in items):
            job.status = JobStatus.cancelled
        elif job.failed:
            job.status = JobStatus.failed
        else:
            job.status = JobStatus.completed
    job.updated_at = utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def mark_item(session: Session, item: JobItem, status: str, action: str = "", error: str | None = None) -> None:
    item.status = status
    item.action = action
    item.error = error
    item.updated_at = utcnow()
    session.add(item)
    session.commit()
    recompute_job(session, item.job_id)


def add_artifact(session: Session, artifact: Artifact, item: JobItem | None = None) -> Artifact:
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    if item:
        item.artifact_id = artifact.id
        item.updated_at = utcnow()
        session.add(item)
        session.commit()
    return artifact

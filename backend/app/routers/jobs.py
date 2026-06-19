from __future__ import annotations

import asyncio
from hmac import compare_digest
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import delete
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.database import get_session
from app.config import settings
from app.events import hub, job_channel
from app.models import Artifact, Job, JobItem, JobKind, JobStatus, LogEvent, utcnow
from app.queue import enqueue_job
from app.schemas import JobRead, VideoJobCreate
from app.services.cookie_snapshots import list_cookie_snapshot_metadata, read_cookie_snapshot, redact_cookie_snapshot_payload
from app.services.jobs import log

router = APIRouter(prefix="/api/video", tags=["video"])


@router.post("/jobs", response_model=JobRead)
def create_video_job(payload: VideoJobCreate, session: Session = Depends(get_session)) -> Job:
    job = Job(kind=JobKind.video, status=JobStatus.queued, title=f"Video batch ({len(payload.prompts)})", total=len(payload.prompts), config_json=payload.model_dump())
    session.add(job)
    session.commit()
    session.refresh(job)
    for prompt in payload.prompts:
        session.add(JobItem(job_id=job.id, prompt=prompt.prompt.strip(), title=prompt.title.strip()))
    session.commit()
    log(session, f"Queued {len(payload.prompts)} video generation item(s).", "info", job.id)
    enqueue_job(JobKind.video, job.id)
    return get_job(job.id, session)


@router.get("/jobs", response_model=list[JobRead])
def list_jobs(session: Session = Depends(get_session)) -> list[Job]:
    return session.exec(select(Job).where(Job.kind == JobKind.video).options(selectinload(Job.items), selectinload(Job.artifacts)).order_by(Job.created_at.desc()).limit(100)).all()


@router.delete("/jobs")
def clear_jobs(session: Session = Depends(get_session)) -> dict[str, int]:
    job_ids = list(session.exec(select(Job.id).where(Job.kind == JobKind.video)).all())
    if not job_ids:
        return {"deleted": 0}
    session.exec(delete(Artifact).where(Artifact.job_id.in_(job_ids)))  # type: ignore[arg-type]
    session.exec(delete(JobItem).where(JobItem.job_id.in_(job_ids)))  # type: ignore[arg-type]
    session.exec(delete(LogEvent).where(LogEvent.job_id.in_(job_ids)))  # type: ignore[arg-type]
    session.exec(delete(Job).where(Job.id.in_(job_ids)))  # type: ignore[arg-type]
    session.commit()
    return {"deleted": len(job_ids)}


@router.get("/jobs/{job_id}/dola-cookie-snapshots")
def list_dola_cookie_snapshots(job_id: UUID, session: Session = Depends(get_session)) -> list[dict]:
    try:
        return list_cookie_snapshot_metadata(session, job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found") from None


@router.get("/jobs/{job_id}/dola-cookie-snapshots/{snapshot_id}")
def get_dola_cookie_snapshot(job_id: UUID, snapshot_id: str, x_admin_token: str = Header(default="", alias="X-Admin-Token"), session: Session = Depends(get_session)) -> dict:
    if not compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=403, detail="Invalid admin token")
    try:
        snapshots = list_cookie_snapshot_metadata(session, job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    metadata = next((snapshot for snapshot in snapshots if snapshot.get("snapshot_id") == snapshot_id), None)
    if not metadata:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    payload = read_cookie_snapshot(metadata)
    return {"metadata": metadata, "snapshot": payload, "redacted": redact_cookie_snapshot_payload(payload)}


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: UUID, session: Session = Depends(get_session)) -> Job:
    job = session.exec(select(Job).where(Job.id == job_id).options(selectinload(Job.items), selectinload(Job.artifacts))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: UUID, session: Session = Depends(get_session)) -> Job:
    job = get_job(job_id, session)
    job.status = JobStatus.cancelled
    job.updated_at = utcnow()
    for item in job.items:
        if item.status in {"queued", "running"}:
            item.status = "cancelled"
            item.action = "Cancelled"
            item.updated_at = utcnow()
            session.add(item)
    session.add(job)
    session.commit()
    log(session, "Cancellation requested.", "warn", job.id)
    return get_job(job_id, session)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: UUID):
    async def stream():
        async for queue in hub.subscribe(job_channel(job_id)):
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/logs")
def logs(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.exec(select(LogEvent).order_by(LogEvent.created_at.desc()).limit(500)).all()
    return [row.model_dump() for row in rows]

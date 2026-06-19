from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.database import get_session
from app.models import Job, JobItem, JobKind, JobStatus
from app.queue import enqueue_job
from app.schemas import ImageJobCreate, JobRead
from app.services.jobs import log

router = APIRouter(prefix="/api/image", tags=["image"])


@router.post("/jobs", response_model=JobRead)
def create_image_job(payload: ImageJobCreate, session: Session = Depends(get_session)) -> Job:
    job = Job(kind=JobKind.image, status=JobStatus.queued, title=f"Image batch ({len(payload.prompts)})", total=len(payload.prompts), config_json=payload.model_dump())
    session.add(job)
    session.commit()
    session.refresh(job)
    for prompt in payload.prompts:
        session.add(JobItem(job_id=job.id, prompt=prompt.strip()))
    session.commit()
    log(session, f"Queued {len(payload.prompts)} image item(s).", "info", job.id)
    enqueue_job(JobKind.image, job.id)
    return get_image_job(job.id, session)


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_image_job(job_id: UUID, session: Session = Depends(get_session)) -> Job:
    job = session.exec(select(Job).where(Job.id == job_id).options(selectinload(Job.items), selectinload(Job.artifacts))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_image_job(job_id: UUID, session: Session = Depends(get_session)) -> Job:
    job = get_image_job(job_id, session)
    job.status = JobStatus.cancelled
    session.add(job)
    session.commit()
    return get_image_job(job_id, session)

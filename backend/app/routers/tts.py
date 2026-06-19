from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import Job, JobItem, JobKind, JobStatus
from app.queue import enqueue_job
from app.schemas import JobRead, TTSJobCreate
from app.services.jobs import log
from app.services.tts import list_voices, synthesize

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/voices")
async def voices() -> list[dict]:
    return await list_voices()


@router.post("/preview")
async def preview(payload: dict) -> dict:
    path = settings.output_dir / "tts-preview.mp3"
    await synthesize(payload.get("text", "AUTO-DOLA preview"), payload.get("voice", settings.tts_default_voice), path)
    return {"path": str(path)}


@router.post("/jobs", response_model=JobRead)
def create_tts_job(payload: TTSJobCreate, session: Session = Depends(get_session)) -> Job:
    job = Job(kind=JobKind.tts, status=JobStatus.queued, title=f"TTS batch ({len(payload.lines)})", total=len(payload.lines), config_json=payload.model_dump())
    session.add(job)
    session.commit()
    session.refresh(job)
    for line in payload.lines:
        session.add(JobItem(job_id=job.id, prompt=line.strip()))
    session.commit()
    log(session, f"Queued {len(payload.lines)} TTS item(s).", "info", job.id)
    enqueue_job(JobKind.tts, job.id)
    return get_tts_job(job.id, session)


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_tts_job(job_id: UUID, session: Session = Depends(get_session)) -> Job:
    job = session.exec(select(Job).where(Job.id == job_id).options(selectinload(Job.items), selectinload(Job.artifacts))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

from __future__ import annotations

import asyncio
import re
import shutil
from hmac import compare_digest
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.database import get_session
from app.config import settings
from app.events import hub, job_channel
from app.models import Artifact, ItemStatus, Job, JobItem, JobKind, JobStatus, LogEvent, utcnow
from app.queue import enqueue_job, get_queue
from app.schemas import JobRead, VideoJobCreate
from app.services.cookie_snapshots import list_cookie_snapshot_metadata, read_cookie_snapshot, redact_cookie_snapshot_payload
from app.services.jobs import log, recompute_job

router = APIRouter(prefix="/api/video", tags=["video"])


def safe_output_folder_name(prompt_count: int, timestamp: str) -> str:
    stamp = re.sub(r"[^0-9-]", "", timestamp)
    return f"{prompt_count}-prompts-{stamp}"


def prepare_video_job_config(payload: VideoJobCreate) -> dict:
    config = payload.model_dump()
    base_folder = Path(config.get("save_folder") or settings.output_dir)
    folder_name = safe_output_folder_name(len(payload.prompts), utcnow().strftime("%Y%m%d-%H%M%S"))
    job_output_folder = base_folder / folder_name
    job_output_folder.mkdir(parents=True, exist_ok=True)
    config["base_save_folder"] = str(base_folder)
    config["job_output_folder"] = str(job_output_folder)
    config["job_output_folder_name"] = folder_name
    return config


def stable_job(job: Job) -> Job:
    job.items = sorted(job.items, key=lambda item: (item.created_at, str(item.id)))
    job.artifacts = sorted(job.artifacts, key=lambda artifact: (artifact.created_at, str(artifact.id)))
    return job


@router.post("/jobs", response_model=JobRead)
def create_video_job(payload: VideoJobCreate, session: Session = Depends(get_session)) -> Job:
    config = prepare_video_job_config(payload)
    job = Job(kind=JobKind.video, status=JobStatus.queued, title=f"Video batch ({len(payload.prompts)})", total=len(payload.prompts), config_json=config)
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
    jobs = session.exec(select(Job).where(Job.kind == JobKind.video).options(selectinload(Job.items), selectinload(Job.artifacts)).order_by(Job.created_at.desc()).limit(100)).all()
    return [stable_job(job) for job in jobs]


@router.delete("/jobs")
def clear_jobs(session: Session = Depends(get_session)) -> dict[str, int]:
    job_ids = list(session.exec(select(Job.id).where(Job.kind == JobKind.video)).all())
    if not job_ids:
        return {"deleted": 0}
    items = list(session.exec(select(JobItem).where(JobItem.job_id.in_(job_ids))).all())  # type: ignore[arg-type]
    slot_ids = {
        str((item.diagnostic_json or {}).get("slot_id") or "")
        for item in items
        if re.fullmatch(r"vpn-slot-[a-f0-9]{32}", str((item.diagnostic_json or {}).get("slot_id") or ""))
    }
    for slot_id in slot_ids:
        shutil.rmtree(settings.log_dir / "vpn-slots" / slot_id, ignore_errors=True)
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
    return stable_job(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: UUID, session: Session = Depends(get_session)) -> Job:
    job = get_job(job_id, session)
    job.status = JobStatus.cancelled
    job.updated_at = utcnow()
    for item in job.items:
        if item.status in {"queued", "running"}:
            item.status = "cancelled"
            item.action = "Force stopped"
            item.updated_at = utcnow()
            session.add(item)
    session.add(job)
    session.commit()
    log(session, "Force stop requested. Worker will stop active polling/downloads and skip queued items.", "warn", job.id)
    return get_job(job_id, session)


@router.post("/jobs/{job_id}/items/{item_id}/resume-poll")
def resume_item_poll(job_id: UUID, item_id: UUID, session: Session = Depends(get_session)) -> dict[str, str | bool]:
    job = get_job(job_id, session)
    item = next((job_item for job_item in job.items if job_item.id == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Video item not found")
    snapshots = list_cookie_snapshot_metadata(session, job_id)
    has_browser_snapshot = any(
        snapshot.get("source") == "browser"
        and str(snapshot.get("item_id")) == str(item_id)
        and snapshot.get("conversation_type") is not None
        for snapshot in snapshots
    )
    if not has_browser_snapshot:
        raise HTTPException(status_code=409, detail="No saved browser session exists for this video item")
    if settings.auto_dola_inline_worker:
        from app.worker import resume_video_item_poll

        resume_video_item_poll(str(job_id), str(item_id))
    else:
        get_queue().enqueue("app.worker.resume_video_item_poll", str(job_id), str(item_id), job_timeout="6h", result_ttl=86400)
    log(session, f"Resume poll queued for video item {item_id}.", "info", job.id)
    return {"ok": True, "queued": True}


@router.post("/jobs/{job_id}/items/{item_id}/force-stop")
def force_stop_item(job_id: UUID, item_id: UUID, session: Session = Depends(get_session)) -> dict[str, str | bool]:
    job = get_job(job_id, session)
    item = next((job_item for job_item in job.items if job_item.id == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Video item not found")
    if item.status not in {ItemStatus.queued, ItemStatus.running}:
        raise HTTPException(status_code=409, detail="Only queued or running video items can be force stopped")
    item.status = ItemStatus.cancelled
    item.action = "Force stopped"
    item.error = None
    item.updated_at = utcnow()
    session.add(item)
    session.commit()
    recompute_job(session, job.id)
    log(session, f"Force stop requested for video item {item_id}.", "warn", job.id)
    return {"ok": True, "stopped": True}


@router.post("/jobs/{job_id}/items/{item_id}/restart")
def restart_item(job_id: UUID, item_id: UUID, session: Session = Depends(get_session)) -> dict[str, str | bool]:
    job = get_job(job_id, session)
    item = next((job_item for job_item in job.items if job_item.id == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Video item not found")
    if item.status not in {ItemStatus.failed, ItemStatus.cancelled, ItemStatus.running, ItemStatus.completed}:
        raise HTTPException(status_code=409, detail="Only failed, cancelled, running, or completed video items can be restarted")
    item.status = ItemStatus.queued
    item.action = "Restart queued"
    item.error = None
    item.diagnostic_json = {}
    item.updated_at = utcnow()
    job.status = JobStatus.queued
    job.updated_at = utcnow()
    session.add(item)
    session.add(job)
    session.commit()
    if settings.auto_dola_inline_worker:
        from app.worker import process_video_item

        process_video_item(str(job_id), str(item_id))
    else:
        get_queue().enqueue("app.worker.process_video_item", str(job_id), str(item_id), job_timeout="6h", result_ttl=86400)
    log(session, f"Restart queued for video item {item_id}. Existing artifact is kept until replacement succeeds.", "info", job.id)
    return {"ok": True, "queued": True}


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
    rows = session.exec(select(LogEvent).order_by(LogEvent.created_at.desc()).limit(5000)).all()
    return [row.model_dump() for row in rows]


@router.get("/browser-screenshots/{filename}")
def browser_screenshot(filename: str) -> FileResponse:
    if not filename.endswith(".png") or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    screenshot_dir = (settings.log_dir / "dola-browser").resolve()
    path = (screenshot_dir / filename).resolve()
    try:
        path.relative_to(screenshot_dir)
    except ValueError:
        raise HTTPException(status_code=404, detail="Screenshot not found") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(path, media_type="image/png", filename=filename)

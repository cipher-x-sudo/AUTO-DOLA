from fastapi import APIRouter, Depends
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import Job, JobKind, LogEvent
from app.schemas import JobRead
from app.services.dola_browser import DolaBrowserClient
from app.services.settings import load_public_settings

router = APIRouter(prefix="/api/studio", tags=["studio"])


@router.get("/status")
async def studio_status(session: Session = Depends(get_session)) -> dict:
    jobs = session.exec(
        select(Job)
        .where(Job.kind == JobKind.video)
        .options(selectinload(Job.items), selectinload(Job.artifacts))
        .order_by(Job.created_at.desc())
        .limit(100)
    ).all()
    for job in jobs:
        job.items = sorted(job.items, key=lambda item: (item.created_at, str(item.id)))
        job.artifacts = sorted(job.artifacts, key=lambda artifact: (artifact.created_at, str(artifact.id)))

    app_settings = load_public_settings(session)
    logs = session.exec(select(LogEvent).order_by(LogEvent.created_at.desc()).limit(1000)).all()
    vpn_enabled = bool(app_settings.get("vpn_enabled"))
    proxy_url = app_settings.get("proxy_url", "") if app_settings.get("proxy_enabled") and not vpn_enabled else ""
    browser_client = DolaBrowserClient(proxy_url=proxy_url)
    try:
        browser = await browser_client.status()
    finally:
        await browser_client.close()
    browser.update(
        {
            "mode": app_settings.get("dola_mode", settings.dola_mode),
            "browser_proxy_active": bool(proxy_url),
            "browser_vpn_enabled": vpn_enabled,
            "browser_headless": bool(app_settings.get("browser_headless")),
        }
    )
    return {
        "jobs": [
            JobRead.model_validate(job, from_attributes=True).model_dump()
            for job in jobs
        ],
        "settings": app_settings,
        "logs": [row.model_dump() for row in logs],
        "browser": browser,
    }

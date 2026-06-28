from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.services.dola import dola_session_status
from app.services.dola_browser import DolaBrowserClient
from app.services.settings import load_public_settings
from app.services.system import chrome_status, ffmpeg_status

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/ffmpeg")
def get_ffmpeg() -> dict:
    return ffmpeg_status()


@router.get("/chrome")
def get_chrome() -> dict:
    return chrome_status()


@router.get("/dola-session")
async def get_dola_session(session: Session = Depends(get_session)) -> dict:
    app_settings = load_public_settings(session)
    return await dola_session_status(app_settings.get("dola_auth_cookies", settings.dola_auth_cookies), settings.dola_default_region)


@router.get("/dola-browser")
async def get_dola_browser(session: Session = Depends(get_session)) -> dict:
    app_settings = load_public_settings(session)
    vpn_enabled = bool(app_settings.get("vpn_enabled"))
    proxy_url = app_settings.get("proxy_url", "") if app_settings.get("proxy_enabled") and not vpn_enabled else ""
    client = DolaBrowserClient(proxy_url=proxy_url)
    try:
        status = await client.status()
        status["mode"] = app_settings.get("dola_mode", settings.dola_mode)
        status["browser_proxy_active"] = bool(proxy_url)
        status["browser_vpn_enabled"] = vpn_enabled
        status["browser_headless"] = bool(app_settings.get("browser_headless"))
        return status
    finally:
        await client.close()


@router.post("/dola-browser/kill-all")
async def kill_all_dola_browser_slots() -> dict:
    client = DolaBrowserClient()
    try:
        return await client.kill_all_slots()
    finally:
        await client.close()

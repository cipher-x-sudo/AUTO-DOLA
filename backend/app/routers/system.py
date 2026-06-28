import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.services.dola import dola_session_status
from app.services.dola_browser import DolaBrowserClient
from app.services.settings import load_public_settings
from app.services.system import chrome_status, ffmpeg_status

router = APIRouter(prefix="/api/system", tags=["system"])
SLOT_ID_RE = re.compile(r"^vpn-slot-[a-f0-9]{32}$")
SLOT_LOG_NAMES = {"docker.log", "openvpn.log", "chromium.log", "browser-manager.log"}


def vpn_slot_log_dir(slot_id: str):
    if not SLOT_ID_RE.fullmatch(slot_id):
        raise HTTPException(status_code=400, detail="Invalid VPN slot ID")
    root = (settings.log_dir / "vpn-slots").resolve()
    path = (root / slot_id).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid VPN slot path")
    return path


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


@router.get("/dola-browser/vpn-slots/{slot_id}/diagnostics")
def get_vpn_slot_diagnostics(slot_id: str) -> dict:
    path = vpn_slot_log_dir(slot_id) / "diagnostic.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="VPN slot diagnostics not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="VPN slot diagnostics are unreadable") from exc
    return payload if isinstance(payload, dict) else {"detail": payload}


@router.get("/dola-browser/vpn-slots/{slot_id}/logs/{log_name}")
def get_vpn_slot_log(slot_id: str, log_name: str) -> FileResponse:
    if log_name not in SLOT_LOG_NAMES:
        raise HTTPException(status_code=400, detail="Invalid VPN slot log name")
    path = vpn_slot_log_dir(slot_id) / log_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="VPN slot log not found")
    return FileResponse(path, media_type="text/plain", filename=f"{slot_id}-{log_name}")

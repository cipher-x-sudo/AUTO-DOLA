from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
import httpx
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.schemas import ProxyTestRequest, SettingsPayload, VpnTestRequest
from app.services.proxy import test_proxy
from app.services.settings import load_app_settings, load_public_settings, save_app_settings
from app.services.vpn import browser_manager_vpn_request, choose_vpn_config, choose_vpn_username, delete_vpn_config, list_vpn_configs, save_vpn_config, vpn_config_path
from app.services.dola_browser import DolaBrowserClient

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings", response_model=SettingsPayload)
def get_settings(session: Session = Depends(get_session)) -> dict:
    return load_public_settings(session)


@router.put("/settings", response_model=SettingsPayload)
def save_settings(payload: SettingsPayload, session: Session = Depends(get_session)) -> dict:
    return save_app_settings(session, payload.model_dump())


@router.post("/proxy/test")
async def proxy_test(payload: ProxyTestRequest) -> dict:
    return await test_proxy(payload.proxy_url)


@router.get("/vpn/configs")
def vpn_configs() -> dict:
    return {"configs": list_vpn_configs()}


@router.post("/vpn/configs")
async def upload_vpn_config(file: UploadFile = File(...)) -> dict:
    try:
        return {"ok": True, "config": await save_vpn_config(file)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/vpn/configs/{name}")
def remove_vpn_config(name: str) -> dict:
    return {"ok": True, "deleted": delete_vpn_config(name)}


@router.get("/vpn/status")
async def vpn_status() -> dict:
    try:
        return await browser_manager_vpn_request(settings.dola_browser_manager_url, "/vpn/status", timeout=10)
    except Exception as exc:
        return {"ok": False, "connected": False, "error": str(exc)}


@router.post("/vpn/test")
async def vpn_test(payload: VpnTestRequest, session: Session = Depends(get_session)) -> dict:
    app_settings = load_app_settings(session, include_secrets=True)
    if not app_settings.get("vpn_password"):
        raise HTTPException(status_code=400, detail="VPN_PASSWORD_MISSING")
    try:
        config = choose_vpn_config(payload.config_name)
        username = choose_vpn_username(str(app_settings.get("vpn_usernames") or ""))
        result = await browser_manager_vpn_request(
            settings.dola_browser_manager_url,
            "/vpn/test-ip",
            {
                "config_path": str(vpn_config_path(config["name"])),
                "config_name": config["name"],
                "username": username,
                "password": app_settings["vpn_password"],
            },
            timeout=180,
        )
        result["username_masked"] = mask_username(username)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/vpn/test-isolated")
async def vpn_test_isolated(payload: VpnTestRequest, session: Session = Depends(get_session)) -> dict:
    app_settings = load_app_settings(session, include_secrets=True)
    if not app_settings.get("vpn_password"):
        raise HTTPException(status_code=400, detail="VPN_PASSWORD_MISSING")
    client = DolaBrowserClient(headless=bool(app_settings.get("browser_headless")))
    slot: dict = {}
    try:
        config = choose_vpn_config(payload.config_name)
        username = choose_vpn_username(str(app_settings.get("vpn_usernames") or ""))
        slot = await client.launch_isolated_vpn_slot(
            config_path=str(vpn_config_path(config["name"])),
            config_name=config["name"],
            username=username,
            password=str(app_settings["vpn_password"]),
        )
        async with httpx.AsyncClient(timeout=60) as http:
            launch_response = await http.post(
                f"{str(slot['manager_url']).rstrip('/')}/launch",
                json={"headless": bool(app_settings.get("browser_headless"))},
            )
            launch_payload = launch_response.json()
            if launch_response.is_error or not launch_payload.get("ok", True):
                raise ValueError(str(launch_payload.get("error") or "CHROMIUM_LAUNCH_FAILED"))
            cdp_url = str(launch_payload.get("container_cdp_url") or launch_payload.get("cdp_url") or "")
            if not cdp_url:
                raise ValueError("CDP_URL_MISSING")
            cdp_response = await http.get(f"{cdp_url.rstrip('/')}/json/version")
            cdp_response.raise_for_status()
        return {
            "ok": True,
            "slot_id": slot.get("slot_id"),
            "config_name": slot.get("config_name"),
            "username_masked": slot.get("username_masked"),
            "ip": slot.get("ip"),
            "cdp": True,
            "log_urls": slot.get("log_urls") or {},
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if slot:
            await client.close_isolated_vpn_slot(slot_id=str(slot.get("slot_id") or ""), container_name=str(slot.get("container_name") or ""))
        await client.close()


def mask_username(username: str) -> str:
    if len(username) <= 3:
        return "***"
    return f"{username[:2]}***{username[-1:]}"

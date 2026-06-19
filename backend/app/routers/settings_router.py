from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.schemas import ProxyTestRequest, SettingsPayload
from app.services.proxy import test_proxy
from app.services.settings import load_public_settings, set_setting

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings", response_model=SettingsPayload)
def get_settings(session: Session = Depends(get_session)) -> dict:
    return load_public_settings(session)


@router.put("/settings", response_model=SettingsPayload)
def save_settings(payload: SettingsPayload, session: Session = Depends(get_session)) -> dict:
    set_setting(session, "app_settings", payload.model_dump())
    return load_public_settings(session)


@router.post("/proxy/test")
async def proxy_test(payload: ProxyTestRequest) -> dict:
    return await test_proxy(payload.proxy_url)

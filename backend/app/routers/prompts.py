from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.database import get_session
from app.schemas import PromptGenerateRequest, PromptGenerateResponse
from app.services.prompts import generate_seedance_prompts
from app.services.settings import load_public_settings

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.post("/generate", response_model=PromptGenerateResponse)
async def generate_prompts(payload: PromptGenerateRequest, session: Session = Depends(get_session)) -> PromptGenerateResponse:
    app_settings = load_public_settings(session)
    try:
        prompts = await generate_seedance_prompts(
            payload.master_prompt,
            payload.count,
            payload.duration,
            payload.ratio,
            payload.style,
            app_settings.get("gemini_api_key", ""),
            app_settings.get("gemini_base_url", ""),
            app_settings.get("gemini_model", "gemini-2.5-flash"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PromptGenerateResponse(prompts=prompts, model=app_settings.get("gemini_model", "gemini-2.5-flash"))

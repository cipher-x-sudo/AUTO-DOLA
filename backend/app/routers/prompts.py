from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.database import get_session
from app.schemas import (
    NichePromptGenerateRequest,
    NichePromptGenerateResponse,
    NichePromptSaveRequest,
    NichePromptSaveResponse,
    PromptGenerateRequest,
    PromptGenerateResponse,
)
from app.services.niches import generate_for_niches, save_niche_prompt_group
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


@router.post("/generate-niches", response_model=NichePromptGenerateResponse)
async def generate_niche_prompts(payload: NichePromptGenerateRequest, session: Session = Depends(get_session)) -> NichePromptGenerateResponse:
    app_settings = load_public_settings(session)
    model = app_settings.get("gemini_model", "gemini-2.5-flash")
    try:
        groups = await generate_for_niches(
            payload.niche_ids,
            payload.count,
            payload.count_mode,
            payload.duration,
            payload.style,
            app_settings.get("gemini_api_key", ""),
            app_settings.get("gemini_base_url", ""),
            model,
            existing_prompts=payload.existing_prompts,
            save=payload.save,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return NichePromptGenerateResponse(groups=groups, model=model)


@router.post("/save-niche-prompts", response_model=NichePromptSaveResponse)
async def save_niche_prompts(payload: NichePromptSaveRequest) -> NichePromptSaveResponse:
    try:
        saved_path = save_niche_prompt_group(payload.niche_id, payload.prompts)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return NichePromptSaveResponse(saved_path=str(saved_path))

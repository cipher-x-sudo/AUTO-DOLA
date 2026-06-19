from fastapi import APIRouter

from app.services.system import chrome_status, ffmpeg_status

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/ffmpeg")
def get_ffmpeg() -> dict:
    return ffmpeg_status()


@router.get("/chrome")
def get_chrome() -> dict:
    return chrome_status()

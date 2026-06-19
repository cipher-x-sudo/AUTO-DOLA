from fastapi import APIRouter

from app.schemas import NicheRead
from app.services.niches import list_niches

router = APIRouter(prefix="/api/niches", tags=["niches"])


@router.get("", response_model=list[NicheRead])
def get_niches() -> list[dict]:
    return [
        {
            "id": niche.id,
            "name": niche.name,
            "filename": niche.filename,
            "size_bytes": niche.size_bytes,
        }
        for niche in list_niches()
    ]

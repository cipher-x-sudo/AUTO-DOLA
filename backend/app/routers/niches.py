from fastapi import APIRouter, HTTPException

from app.schemas import NicheDeleteResponse, NicheRead
from app.services.niches import delete_niche, list_niches

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


@router.delete("/{niche_id}", response_model=NicheDeleteResponse)
def remove_niche(niche_id: str) -> NicheDeleteResponse:
    try:
        niche = delete_niche(niche_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return NicheDeleteResponse(deleted=True, niche_id=niche.id)

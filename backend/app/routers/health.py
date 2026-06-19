from fastapi import APIRouter, Depends
from sqlmodel import Session, text

from app.config import settings
from app.database import get_session
from app.schemas import HealthRead

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthRead)
def health(session: Session = Depends(get_session)) -> HealthRead:
    session.exec(text("SELECT 1"))
    return HealthRead(ok=True, service=settings.app_name, environment=settings.environment, database="ok")

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from app.database import get_session
from app.models import Artifact

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}/download")
def download_artifact(artifact_id: UUID, session: Session = Depends(get_session)) -> FileResponse:
    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = Path(artifact.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact file is missing")
    return FileResponse(path, media_type=artifact.mime_type, filename=artifact.filename)

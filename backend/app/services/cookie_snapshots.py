from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

from sqlmodel import Session

from app.config import settings
from app.models import Job, utcnow
from app.services.dola import DolaSession, cookie_names_from_header
from app.services.settings import decrypt_value, encrypt_value


SNAPSHOT_DIR_NAME = "dola-cookie-snapshots"


def snapshot_root(base_dir: Path | None = None) -> Path:
    root = (base_dir or settings.log_dir) / SNAPSHOT_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_cookie_snapshot(
    session: Session,
    job_id: UUID,
    item_id: UUID,
    attempt: int,
    dola_session: DolaSession,
    *,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    cookie_header = dola_session.headers.get("cookie", "")
    cookie_names = cookie_names_from_header(cookie_header)
    snapshot_id = str(uuid4())
    created_at = utcnow()
    path = snapshot_root(base_dir) / f"{job_id}-{item_id}-{attempt}-{snapshot_id}.json.enc"
    payload = {
        "snapshot_id": snapshot_id,
        "job_id": str(job_id),
        "item_id": str(item_id),
        "attempt": attempt,
        "created_at": created_at.isoformat(),
        "cookie_header": cookie_header,
        "dola_url": dola_session.url,
        "dola_url_query": parse_qs(urlparse(dola_session.url).query),
        "fp": dola_session.fp,
        "has_ttwid": dola_session.has_ttwid,
        "has_hook_slardar": dola_session.has_hook_slardar,
        "has_auth_cookies": dola_session.has_auth_cookies,
    }
    path.write_text(encrypt_value(payload), encoding="utf-8")
    metadata = {
        "snapshot_id": snapshot_id,
        "job_id": str(job_id),
        "item_id": str(item_id),
        "attempt": attempt,
        "created_at": created_at.isoformat(),
        "encrypted_file_path": str(path),
        "cookie_sha256": hashlib.sha256(cookie_header.encode()).hexdigest(),
        "cookie_names": cookie_names,
        "cookie_count": len(cookie_names),
        "has_ttwid": dola_session.has_ttwid,
        "has_hook_slardar": dola_session.has_hook_slardar,
        "has_auth_cookies": dola_session.has_auth_cookies,
        "region": settings.dola_default_region,
        "conversation_id_masked": None,
        "conversation_type": None,
    }
    append_cookie_snapshot_metadata(session, job_id, metadata)
    return metadata


def append_cookie_snapshot_metadata(session: Session, job_id: UUID, metadata: dict[str, Any]) -> None:
    job = session.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    snapshots = list(job.dola_cookie_snapshots_json or [])
    snapshots.append(metadata)
    job.dola_cookie_snapshots_json = snapshots
    job.updated_at = utcnow()
    session.add(job)
    session.commit()


def mark_cookie_snapshot_conversation(session: Session, job_id: UUID, snapshot_id: str, conversation_id: str, conversation_type: int) -> None:
    job = session.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    snapshots = list(job.dola_cookie_snapshots_json or [])
    for snapshot in snapshots:
        if snapshot.get("snapshot_id") == snapshot_id:
            snapshot["conversation_id_masked"] = mask_conversation_id(conversation_id)
            snapshot["conversation_type"] = conversation_type
            snapshot["conversation_recorded_at"] = utcnow().isoformat()
            break
    job.dola_cookie_snapshots_json = snapshots
    job.updated_at = utcnow()
    session.add(job)
    session.commit()


def list_cookie_snapshot_metadata(session: Session, job_id: UUID) -> list[dict[str, Any]]:
    job = session.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    return list(job.dola_cookie_snapshots_json or [])


def read_cookie_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(metadata["encrypted_file_path"]))
    return decrypt_value(path.read_text(encoding="utf-8"))


def mask_conversation_id(conversation_id: str) -> str:
    return f"*{conversation_id[-8:]}" if len(conversation_id) > 8 else f"*{conversation_id}"


def redact_cookie_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(payload)
    cookie_header = str(redacted.pop("cookie_header", ""))
    redacted["cookie_names"] = cookie_names_from_header(cookie_header)
    redacted["cookie_count"] = len(redacted["cookie_names"])
    redacted["cookie_sha256"] = hashlib.sha256(cookie_header.encode()).hexdigest()
    redacted["redacted_at"] = utcnow().isoformat()
    return redacted

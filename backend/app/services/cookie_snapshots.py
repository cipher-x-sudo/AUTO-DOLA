from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

from sqlalchemy.orm.attributes import flag_modified
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
    source: str = "direct",
    chat_url: str = "",
    extra_metadata: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cookie_header = dola_session.headers.get("cookie", "")
    cookie_names = cookie_names_from_header(cookie_header)
    snapshot_id = str(uuid4())
    created_at = utcnow()
    path = (snapshot_root(base_dir) / f"{job_id}-{item_id}-{attempt}-{snapshot_id}.json.enc").resolve()
    payload = {
        "snapshot_id": snapshot_id,
        "job_id": str(job_id),
        "item_id": str(item_id),
        "attempt": attempt,
        "created_at": created_at.isoformat(),
        "cookie_header": cookie_header,
        "headers": dola_session.headers,
        "payload_template": dola_session.payload_template,
        "dola_url": dola_session.url,
        "dola_url_query": parse_qs(urlparse(dola_session.url).query),
        "fp": dola_session.fp,
        "has_ttwid": dola_session.has_ttwid,
        "has_hook_slardar": dola_session.has_hook_slardar,
        "has_auth_cookies": dola_session.has_auth_cookies,
        "source": source,
        "chat_url": chat_url,
    }
    if extra_payload:
        payload.update(extra_payload)
    path.write_text(encrypt_value(payload), encoding="utf-8")
    metadata = {
        "snapshot_id": snapshot_id,
        "job_id": str(job_id),
        "item_id": str(item_id),
        "attempt": attempt,
        "source": source,
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
        "chat_url": chat_url,
        "raw_response_events": [],
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    append_cookie_snapshot_metadata(session, job_id, metadata)
    return metadata


def append_cookie_snapshot_metadata(session: Session, job_id: UUID, metadata: dict[str, Any]) -> None:
    job = session.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    snapshots = list(job.dola_cookie_snapshots_json or [])
    snapshots.append(metadata)
    save_job_snapshots(session, job, snapshots)


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
            payload = read_cookie_snapshot(snapshot)
            payload["conversation_id"] = conversation_id
            payload["conversation_type"] = conversation_type
            Path(str(snapshot["encrypted_file_path"])).write_text(encrypt_value(payload), encoding="utf-8")
            break
    save_job_snapshots(session, job, snapshots)


def update_cookie_snapshot(
    session: Session,
    job_id: UUID,
    snapshot_id: str,
    *,
    metadata_updates: dict[str, Any] | None = None,
    payload_updates: dict[str, Any] | None = None,
) -> None:
    job = session.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    snapshots = list(job.dola_cookie_snapshots_json or [])
    for snapshot in snapshots:
        if snapshot.get("snapshot_id") == snapshot_id:
            if metadata_updates:
                snapshot.update(metadata_updates)
            if payload_updates:
                payload = read_cookie_snapshot(snapshot)
                payload.update(payload_updates)
                Path(str(snapshot["encrypted_file_path"])).write_text(encrypt_value(payload), encoding="utf-8")
            break
    else:
        raise ValueError(f"Cookie snapshot {snapshot_id} not found for job {job_id}")
    save_job_snapshots(session, job, snapshots)


def latest_browser_snapshot_for_item(session: Session, job_id: UUID, item_id: UUID) -> dict[str, Any] | None:
    snapshots = list_cookie_snapshot_metadata(session, job_id)
    browser_snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.get("source") == "browser"
        and str(snapshot.get("item_id")) == str(item_id)
        and snapshot.get("conversation_type") is not None
    ]
    return browser_snapshots[-1] if browser_snapshots else None


def dola_session_from_cookie_snapshot(metadata: dict[str, Any]) -> tuple[DolaSession, dict[str, Any]]:
    payload = read_cookie_snapshot(metadata)
    headers = dict(payload.get("headers") or {})
    cookie_header = str(payload.get("cookie_header") or "")
    if cookie_header:
        headers["cookie"] = cookie_header
    dola_session = DolaSession(
        url=str(payload.get("dola_url") or ""),
        headers=headers,
        payload_template=dict(payload.get("payload_template") or {}),
        fp=str(payload.get("fp") or ""),
        has_ttwid=bool(payload.get("has_ttwid")),
        has_hook_slardar=bool(payload.get("has_hook_slardar")),
        has_auth_cookies=bool(payload.get("has_auth_cookies")),
    )
    return dola_session, payload


def append_raw_response_event(
    session: Session,
    job_id: UUID,
    snapshot_id: str,
    response_type: str,
    attempt: int,
    status_code: int,
    body: str,
) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    created_at = utcnow()
    body_bytes = body.encode("utf-8")
    event_metadata = {
        "response_type": response_type,
        "attempt": attempt,
        "status_code": status_code,
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "body_bytes": len(body_bytes),
        "created_at": created_at.isoformat(),
        "snapshot_id": snapshot_id,
    }

    snapshots = list(job.dola_cookie_snapshots_json or [])
    target_snapshot: dict[str, Any] | None = None
    for snapshot in snapshots:
        if snapshot.get("snapshot_id") == snapshot_id:
            target_snapshot = snapshot
            break
    if target_snapshot is None:
        raise ValueError(f"Cookie snapshot {snapshot_id} not found for job {job_id}")

    events = list(target_snapshot.get("raw_response_events") or [])
    event_metadata["event_index"] = len(events) + 1
    events.append(event_metadata)
    target_snapshot["raw_response_events"] = events

    payload = read_cookie_snapshot(target_snapshot)
    payload_events = list(payload.get("raw_response_events") or [])
    payload_events.append({**event_metadata, "body": body})
    payload["raw_response_events"] = payload_events
    Path(str(target_snapshot["encrypted_file_path"])).write_text(encrypt_value(payload), encoding="utf-8")

    save_job_snapshots(session, job, snapshots)
    return event_metadata


def save_job_snapshots(session: Session, job: Job, snapshots: list[dict[str, Any]]) -> None:
    job.dola_cookie_snapshots_json = snapshots
    flag_modified(job, "dola_cookie_snapshots_json")
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
    headers = dict(redacted.pop("headers", {}) or {})
    redacted["cookie_names"] = cookie_names_from_header(cookie_header)
    redacted["cookie_count"] = len(redacted["cookie_names"])
    redacted["cookie_sha256"] = hashlib.sha256(cookie_header.encode()).hexdigest()
    redacted["header_names"] = sorted(headers)
    redacted["redacted_at"] = utcnow().isoformat()
    return redacted

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlmodel import Session

from app.database import get_session
from app.models import DolaCookieProfile, utcnow
from app.schemas import CookieProfileUpdate
from app.services.cookie_profiles import (
    bulk_import_profiles,
    cookie_header_from_encrypted,
    create_profile,
    find_profile_by_name,
    list_profiles,
    parse_cookie_json,
    ParsedCookieSet,
    profile_metadata,
    replace_profile,
    update_validation,
)
from app.services.dola import dola_session_status

router = APIRouter(prefix="/api/dola-cookie-profiles", tags=["dola-cookie-profiles"])


def _validate_input(name: str, daily_limit: int) -> str:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Profile name is required.")
    if daily_limit < 1 or daily_limit > 10000:
        raise HTTPException(status_code=400, detail="Daily limit must be between 1 and 10000.")
    return clean_name


async def _read_parsed(file: UploadFile | None, cookies_json: str | None) -> tuple[ParsedCookieSet, str]:
    if file is not None:
        raw = await file.read()
    elif cookies_json and cookies_json.strip():
        raw = cookies_json.encode("utf-8")
    else:
        raise HTTPException(status_code=400, detail="Provide a cookie JSON file or pasted JSON.")
    try:
        parsed = parse_cookie_json(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return parsed, parsed.cookie_header


async def _validate_profile(session: Session, profile: DolaCookieProfile, cookie_header: str) -> None:
    try:
        status = await dola_session_status(cookie_header)
        ok = bool(status.get("ok") and status.get("has_auth_cookies"))
        error = "" if ok else str(status.get("error") or "Dola did not confirm authenticated cookies.")
    except Exception as exc:
        ok = False
        error = str(exc)
    update_validation(session, profile, ok=ok, error=error)


def _extract_bulk_profiles(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    profiles = payload.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise HTTPException(status_code=400, detail="Bulk JSON must include a non-empty profiles array.")
    return profiles, bool(payload.get("validate", False))


async def _parse_bulk_request(request: Request) -> tuple[list[dict[str, Any]], bool]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        do_validate = str(form.get("validate") or "").lower() in {"1", "true", "yes"}
        if upload is None:
            raise HTTPException(status_code=400, detail="Multipart bulk import requires a file field.")
        raw = await upload.read()  # type: ignore[union-attr]
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail="Bulk file must contain valid JSON.") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Bulk file must be a JSON object.")
        items, file_validate = _extract_bulk_profiles(data)
        return items, do_validate or file_validate

    try:
        data = await request.json()
    except Exception:
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="Provide a JSON body or bulk file upload.")
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Provide a JSON body or bulk file upload.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Bulk body must be a JSON object.")
    return _extract_bulk_profiles(data)


@router.get("")
def get_cookie_profiles(session: Session = Depends(get_session)) -> list[dict]:
    return list_profiles(session)


@router.post("/import")
async def import_cookie_profile(
    file: UploadFile | None = File(None),
    cookies_json: str | None = Form(None),
    name: str = Form(...),
    daily_limit: int = Form(3),
    session: Session = Depends(get_session),
) -> dict:
    clean_name = _validate_input(name, daily_limit)
    parsed, cookie_header = await _read_parsed(file, cookies_json)
    profile = create_profile(session, clean_name, daily_limit, parsed)
    await _validate_profile(session, profile, cookie_header)
    return profile_metadata(session, profile)


@router.post("/import/bulk")
async def import_cookie_profiles_bulk(request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Accept application/json body or multipart file (auto_dola_bulk_import.json)."""
    items, do_validate = await _parse_bulk_request(request)
    result = bulk_import_profiles(session, items, validate=False)
    if do_validate:
        for item in items:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if any(f.get("name") == name for f in (result.failed or [])):
                continue
            profile = find_profile_by_name(session, name)
            if profile:
                await _validate_profile(session, profile, cookie_header_from_encrypted(profile.cookies_encrypted))
    return result.as_dict()


@router.put("/{profile_id}")
def update_cookie_profile(profile_id: UUID, payload: CookieProfileUpdate, session: Session = Depends(get_session)) -> dict:
    profile = session.get(DolaCookieProfile, profile_id)
    if not profile or profile.deleted_at:
        raise HTTPException(status_code=404, detail="Cookie profile not found.")
    if payload.name is not None:
        profile.name = _validate_input(payload.name, payload.daily_limit or profile.daily_limit)
    if payload.daily_limit is not None:
        _validate_input(profile.name, payload.daily_limit)
        profile.daily_limit = payload.daily_limit
    if payload.enabled is not None:
        profile.enabled = payload.enabled
    profile.updated_at = utcnow()
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile_metadata(session, profile)


@router.post("/{profile_id}/replace")
async def replace_cookie_profile(
    profile_id: UUID,
    file: UploadFile | None = File(None),
    cookies_json: str | None = Form(None),
    name: str | None = Form(None),
    daily_limit: int | None = Form(None),
    session: Session = Depends(get_session),
) -> dict:
    profile = session.get(DolaCookieProfile, profile_id)
    if not profile or profile.deleted_at:
        raise HTTPException(status_code=404, detail="Cookie profile not found.")
    if daily_limit is not None:
        _validate_input(name or profile.name, daily_limit)
    parsed, cookie_header = await _read_parsed(file, cookies_json)
    updated = replace_profile(session, profile, parsed, name=name, daily_limit=daily_limit)
    await _validate_profile(session, updated, cookie_header)
    return profile_metadata(session, updated)


@router.post("/{profile_id}/test")
async def test_cookie_profile(profile_id: UUID, session: Session = Depends(get_session)) -> dict:
    profile = session.get(DolaCookieProfile, profile_id)
    if not profile or profile.deleted_at:
        raise HTTPException(status_code=404, detail="Cookie profile not found.")
    await _validate_profile(session, profile, cookie_header_from_encrypted(profile.cookies_encrypted))
    return profile_metadata(session, profile)


@router.delete("/{profile_id}")
def delete_cookie_profile(profile_id: UUID, session: Session = Depends(get_session)) -> dict[str, bool]:
    profile = session.get(DolaCookieProfile, profile_id)
    if not profile or profile.deleted_at:
        raise HTTPException(status_code=404, detail="Cookie profile not found.")
    profile.deleted_at = utcnow()
    profile.enabled = False
    profile.updated_at = utcnow()
    session.add(profile)
    session.commit()
    return {"deleted": True}

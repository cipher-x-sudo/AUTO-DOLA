from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlmodel import Session, select

from app.models import DolaCookieJobSnapshot, DolaCookieProfile, DolaCookieUsage, utcnow
from app.services.settings import decrypt_value, encrypt_value


COOKIE_TIMEZONE = ZoneInfo("Asia/Karachi")
MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ParsedCookieSet:
    cookies: list[dict[str, Any]]
    cookie_header: str
    names: list[str]


@dataclass(frozen=True)
class CookieReservation:
    snapshot: DolaCookieJobSnapshot
    usage_day: str


def cookie_usage_day(now: datetime | None = None) -> str:
    current = now or datetime.now(COOKIE_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=COOKIE_TIMEZONE)
    return current.astimezone(COOKIE_TIMEZONE).date().isoformat()


def _domain_is_dola(domain: str) -> bool:
    normalized = domain.lower().strip().lstrip(".")
    return normalized in {"dola.com", "www.dola.com"} or normalized.endswith(".dola.com")


def _expiry_value(item: dict[str, Any]) -> float | None:
    raw = item.get("expirationDate", item.get("expires"))
    if raw in (None, "", -1):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _normalize_cookie(name: Any, value: Any, item: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cookie = item or {}
    clean_name = str(name or cookie.get("name") or "").strip()
    clean_value = str(value if value is not None else cookie.get("value") or "")
    if not clean_name or not clean_value:
        return None
    domain = str(cookie.get("domain") or "www.dola.com").strip().lower()
    if not _domain_is_dola(domain):
        return None
    if domain == "dola.com":
        domain = ".dola.com"
    elif not domain.startswith("."):
        domain = f".{domain}"
    expires = _expiry_value(cookie)
    if expires is not None and expires <= datetime.now().timestamp():
        return None
    normalized: dict[str, Any] = {
        "name": clean_name,
        "value": clean_value,
        "domain": domain,
        "path": str(cookie.get("path") or "/"),
        "secure": bool(cookie.get("secure", True)),
        "httpOnly": bool(cookie.get("httpOnly", False)),
    }
    if expires is not None:
        normalized["expires"] = expires
    same_site = cookie.get("sameSite")
    if same_site in {"Strict", "Lax", "None"}:
        normalized["sameSite"] = same_site
    return normalized


def parse_cookie_json(raw: bytes | str) -> ParsedCookieSet:
    if isinstance(raw, bytes):
        if len(raw) > MAX_COOKIE_FILE_BYTES:
            raise ValueError("Cookie JSON file is too large.")
        text = raw.decode("utf-8-sig")
    else:
        text = raw
    try:
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cookie file must contain valid JSON.") from exc

    cookies: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_cookie(item.get("name"), item.get("value"), item)
            if normalized:
                cookies.append(normalized)
    elif isinstance(payload, dict):
        # Support both {"name": "value"} and a single browser-cookie object.
        if "name" in payload and "value" in payload:
            normalized = _normalize_cookie(payload.get("name"), payload.get("value"), payload)
            if normalized:
                cookies.append(normalized)
        else:
            for name, value in payload.items():
                normalized = _normalize_cookie(name, value)
                if normalized:
                    cookies.append(normalized)
    else:
        raise ValueError("Cookie JSON must be an array or object map.")

    deduped: dict[str, dict[str, Any]] = {}
    for cookie in cookies:
        deduped[cookie["name"]] = cookie
    cookies = list(deduped.values())
    if not cookies:
        raise ValueError("No non-expired Dola cookies were found in the JSON file.")
    names = sorted(cookie["name"] for cookie in cookies)
    header = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
    return ParsedCookieSet(cookies=cookies, cookie_header=header, names=names)


def cookie_header_from_encrypted(token: str) -> str:
    payload = decrypt_value(token)
    if not isinstance(payload, list):
        raise ValueError("Stored cookie payload is invalid.")
    parsed = parse_cookie_json(json.dumps(payload))
    return parsed.cookie_header


def cookies_from_encrypted(token: str) -> list[dict[str, Any]]:
    payload = decrypt_value(token)
    if not isinstance(payload, list):
        raise ValueError("Stored cookie payload is invalid.")
    return parse_cookie_json(json.dumps(payload)).cookies


def _usage(session: Session, profile_id: UUID, usage_day: str) -> DolaCookieUsage:
    row = session.exec(
        select(DolaCookieUsage).where(
            DolaCookieUsage.profile_id == profile_id,
            DolaCookieUsage.usage_day == usage_day,
        )
    ).first()
    if row:
        return row
    row = DolaCookieUsage(profile_id=profile_id, usage_day=usage_day)
    session.add(row)
    session.flush()
    return row


def profile_metadata(session: Session, profile: DolaCookieProfile, *, include_deleted: bool = False) -> dict[str, Any]:
    day = cookie_usage_day()
    usage = session.exec(
        select(DolaCookieUsage).where(
            DolaCookieUsage.profile_id == profile.id,
            DolaCookieUsage.usage_day == day,
        )
    ).first()
    completed = int(usage.completed_count if usage else 0)
    reserved = int(usage.reserved_count if usage else 0)
    return {
        "id": str(profile.id),
        "name": profile.name,
        "cookie_names": list(profile.cookie_names_json or []),
        "cookie_count": len(profile.cookie_names_json or []),
        "daily_limit": profile.daily_limit,
        "enabled": profile.enabled,
        "validation_status": profile.validation_status,
        "validation_error": profile.validation_error,
        "usage_day": day,
        "completed_today": completed,
        "reserved_today": reserved,
        "remaining_today": max(0, profile.daily_limit - completed - reserved),
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
        "deleted": bool(profile.deleted_at),
    }


def list_profiles(session: Session) -> list[dict[str, Any]]:
    profiles = session.exec(
        select(DolaCookieProfile)
        .where(DolaCookieProfile.deleted_at.is_(None))
        .order_by(DolaCookieProfile.created_at.asc())
    ).all()
    return [profile_metadata(session, profile) for profile in profiles]


def create_profile(session: Session, name: str, daily_limit: int, parsed: ParsedCookieSet) -> DolaCookieProfile:
    profile = DolaCookieProfile(
        name=name.strip(),
        cookies_encrypted=encrypt_value(parsed.cookies),
        cookie_names_json=parsed.names,
        daily_limit=daily_limit,
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


def replace_profile(session: Session, profile: DolaCookieProfile, parsed: ParsedCookieSet, *, name: str | None = None, daily_limit: int | None = None) -> DolaCookieProfile:
    profile.cookies_encrypted = encrypt_value(parsed.cookies)
    profile.cookie_names_json = parsed.names
    profile.validation_status = "pending"
    profile.validation_error = None
    if name and name.strip():
        profile.name = name.strip()
    if daily_limit is not None:
        profile.daily_limit = daily_limit
    profile.updated_at = utcnow()
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


def update_validation(session: Session, profile: DolaCookieProfile, *, ok: bool, error: str = "") -> None:
    profile.validation_status = "valid" if ok else "invalid"
    profile.validation_error = error[:500] if error else None
    profile.updated_at = utcnow()
    session.add(profile)
    session.commit()


def snapshot_profiles(session: Session, job_id: UUID, profile_ids: list[UUID]) -> list[DolaCookieJobSnapshot]:
    snapshots: list[DolaCookieJobSnapshot] = []
    seen: set[UUID] = set()
    for priority, profile_id in enumerate(profile_ids):
        if profile_id in seen:
            continue
        seen.add(profile_id)
        profile = session.get(DolaCookieProfile, profile_id)
        if not profile or profile.deleted_at or not profile.enabled:
            raise ValueError(f"Cookie profile is unavailable: {profile_id}")
        snapshot = DolaCookieJobSnapshot(
            job_id=job_id,
            profile_id=profile.id,
            profile_name=profile.name,
            priority=priority,
            daily_limit=profile.daily_limit,
            cookies_encrypted=profile.cookies_encrypted,
            cookie_names_json=list(profile.cookie_names_json or []),
        )
        session.add(snapshot)
        _usage(session, profile.id, cookie_usage_day())
        snapshots.append(snapshot)
    session.flush()
    return snapshots


def job_snapshots(session: Session, job_id: UUID) -> list[DolaCookieJobSnapshot]:
    return session.exec(
        select(DolaCookieJobSnapshot)
        .where(DolaCookieJobSnapshot.job_id == job_id)
        .order_by(DolaCookieJobSnapshot.priority.asc())
    ).all()


def reserve_profile(session: Session, snapshots: list[DolaCookieJobSnapshot]) -> CookieReservation | None:
    if not snapshots:
        return None
    day = cookie_usage_day()
    for snapshot in snapshots:
        _usage(session, snapshot.profile_id, day)
        result = session.exec(
            update(DolaCookieUsage)
            .where(
                DolaCookieUsage.profile_id == snapshot.profile_id,
                DolaCookieUsage.usage_day == day,
                (DolaCookieUsage.completed_count + DolaCookieUsage.reserved_count) < snapshot.daily_limit,
            )
            .values(reserved_count=DolaCookieUsage.reserved_count + 1, updated_at=utcnow())
        )
        if result.rowcount == 1:
            session.commit()
            return CookieReservation(snapshot=snapshot, usage_day=day)
        session.rollback()
    session.rollback()
    return None


def complete_profile_reservation(session: Session, reservation: CookieReservation) -> None:
    usage = session.exec(
        select(DolaCookieUsage).where(
            DolaCookieUsage.profile_id == reservation.snapshot.profile_id,
            DolaCookieUsage.usage_day == reservation.usage_day,
        )
    ).first()
    if usage:
        usage.reserved_count = max(0, usage.reserved_count - 1)
        usage.completed_count += 1
        usage.updated_at = utcnow()
        session.add(usage)
        session.commit()


def release_profile_reservation(session: Session, reservation: CookieReservation | None) -> None:
    if not reservation:
        return
    usage = session.exec(
        select(DolaCookieUsage).where(
            DolaCookieUsage.profile_id == reservation.snapshot.profile_id,
            DolaCookieUsage.usage_day == reservation.usage_day,
        )
    ).first()
    if usage:
        usage.reserved_count = max(0, usage.reserved_count - 1)
        usage.updated_at = utcnow()
        session.add(usage)
        session.commit()

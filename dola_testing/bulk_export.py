"""Write per-UID Dola cookie profiles and a bulk AUTO-DOLA import JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_uid_profile(profiles_dir: Path, c_user: str, cookies: list[dict[str, Any]]) -> Path:
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{c_user}.json"
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    return path


def build_bulk_payload(
    *,
    profiles: list[dict[str, Any]],
    failures: list[dict[str, str]],
    default_daily_limit: int = 2,
) -> dict[str, Any]:
    return {
        "version": 1,
        "default_daily_limit": default_daily_limit,
        "profiles": profiles,
        "failures": failures,
    }


def write_bulk_import(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def profile_entry(
    *,
    name: str,
    cookies: list[dict[str, Any]],
    daily_limit: int = 2,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "daily_limit": daily_limit,
        "enabled": enabled,
        "cookies": cookies,
    }

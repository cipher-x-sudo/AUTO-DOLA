from __future__ import annotations

import base64
import json
from hashlib import sha256
from typing import Any

from cryptography.fernet import Fernet
from sqlmodel import Session

from app.config import settings
from app.models import Setting, utcnow


def _fernet() -> Fernet:
    if settings.settings_encryption_key:
        key = settings.settings_encryption_key.encode()
    else:
        key = base64.urlsafe_b64encode(sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt_value(value: Any) -> str:
    return _fernet().encrypt(json.dumps(value).encode()).decode()


def decrypt_value(token: str) -> Any:
    return json.loads(_fernet().decrypt(token.encode()).decode())


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, key)
    return default if not row else decrypt_value(row.value_encrypted)


def set_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key) or Setting(key=key, value_encrypted="")
    row.value_encrypted = encrypt_value(value)
    row.updated_at = utcnow()
    session.add(row)
    session.commit()


def _default_settings() -> dict[str, Any]:
    return {
        "dola_auth_cookies": settings.dola_auth_cookies,
        "yousmind_api_key": settings.yousmind_api_key,
        "gemini_api_key": settings.gemini_api_key,
        "gemini_base_url": settings.gemini_base_url,
        "gemini_model": settings.gemini_model,
        "default_ratio": "9:16",
        "default_duration": 10,
        "default_parallel": 5,
        "output_dir": str(settings.output_dir),
        "proxy_enabled": False,
        "proxy_url": "",
        "vpn_enabled": False,
        "vpn_usernames": "",
        "vpn_password": "",
        "vpn_password_saved": False,
        "tts_default_voice": settings.tts_default_voice,
        "dola_mode": settings.dola_mode if settings.dola_mode in {"direct", "browser", "hybrid"} else "hybrid",
    }


def load_app_settings(session: Session, *, include_secrets: bool = False) -> dict[str, Any]:
    loaded = {**_default_settings(), **get_setting(session, "app_settings", {})}
    loaded["vpn_password_saved"] = bool(loaded.get("vpn_password"))
    if not include_secrets:
        loaded["vpn_password"] = ""
    return loaded


def save_app_settings(session: Session, value: dict[str, Any]) -> dict[str, Any]:
    current = load_app_settings(session, include_secrets=True)
    next_value = {**current, **value}
    if not value.get("vpn_password") and current.get("vpn_password"):
        next_value["vpn_password"] = current.get("vpn_password", "")
    next_value.pop("vpn_password_saved", None)
    set_setting(session, "app_settings", next_value)
    return load_app_settings(session)


def load_public_settings(session: Session) -> dict[str, Any]:
    return load_app_settings(session)


def _legacy_load_public_settings(session: Session) -> dict[str, Any]:
    defaults = {
        "dola_auth_cookies": settings.dola_auth_cookies,
        "yousmind_api_key": settings.yousmind_api_key,
        "gemini_api_key": settings.gemini_api_key,
        "gemini_base_url": settings.gemini_base_url,
        "gemini_model": settings.gemini_model,
        "default_ratio": "9:16",
        "default_duration": 10,
        "default_parallel": 5,
        "output_dir": str(settings.output_dir),
        "proxy_enabled": False,
        "proxy_url": "",
        "tts_default_voice": settings.tts_default_voice,
        "dola_mode": settings.dola_mode if settings.dola_mode in {"direct", "browser", "hybrid"} else "hybrid",
    }
    return {**defaults, **get_setting(session, "app_settings", {})}

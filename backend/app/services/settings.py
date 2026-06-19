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


def load_public_settings(session: Session) -> dict[str, Any]:
    defaults = {
        "dola_auth_cookies": settings.dola_auth_cookies,
        "yousmind_api_key": settings.yousmind_api_key,
        "gemini_api_key": settings.gemini_api_key,
        "gemini_base_url": settings.gemini_base_url,
        "gemini_model": settings.gemini_model,
        "default_ratio": "9:16",
        "default_duration": 15,
        "default_parallel": 5,
        "output_dir": str(settings.output_dir),
        "proxy_enabled": False,
        "proxy_url": "",
        "tts_default_voice": settings.tts_default_voice,
    }
    return {**defaults, **get_setting(session, "app_settings", {})}

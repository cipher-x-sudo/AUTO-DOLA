from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import UploadFile

from app.config import settings


VPN_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def vpn_root() -> Path:
    root = settings.vpn_dir
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def safe_vpn_name(name: str) -> str:
    cleaned = VPN_NAME_RE.sub("_", Path(name).name).strip("._")
    if not cleaned.lower().endswith(".ovpn"):
        cleaned = f"{cleaned}.ovpn"
    return cleaned or "config.ovpn"


def vpn_config_path(name: str) -> Path:
    path = (vpn_root() / safe_vpn_name(name)).resolve()
    if vpn_root() not in path.parents:
        raise ValueError("Invalid VPN config path.")
    return path


def list_vpn_configs() -> list[dict[str, Any]]:
    return [
        {"name": path.name, "size_bytes": path.stat().st_size}
        for path in sorted(vpn_root().glob("*.ovpn"))
        if path.is_file()
    ]


async def save_vpn_config(file: UploadFile) -> dict[str, Any]:
    name = safe_vpn_name(file.filename or "config.ovpn")
    path = vpn_config_path(name)
    content = await file.read()
    if not content:
        raise ValueError("VPN config file is empty.")
    path.write_bytes(content)
    return {"name": path.name, "size_bytes": path.stat().st_size}


def delete_vpn_config(name: str) -> bool:
    path = vpn_config_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def choose_vpn_config(config_name: str = "", excluded_names: set[str] | None = None) -> dict[str, Any]:
    excluded = excluded_names or set()
    configs = [config for config in list_vpn_configs() if config["name"] not in excluded]
    if not configs:
        raise ValueError("VPN_CONFIG_MISSING")
    if config_name:
        selected = next((config for config in configs if config["name"] == safe_vpn_name(config_name)), None)
        if not selected:
            raise ValueError("VPN_CONFIG_MISSING")
        return selected
    return random.choice(configs)


def choose_vpn_username(usernames: str) -> str:
    candidates = [line.strip() for line in usernames.replace(",", "\n").splitlines() if line.strip()]
    if not candidates:
        raise ValueError("VPN_USERNAME_MISSING")
    return random.choice(candidates)


async def browser_manager_vpn_request(manager_url: str, endpoint: str, payload: dict[str, Any] | None = None, timeout: float = 90) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{manager_url.rstrip('/')}{endpoint}", json=payload or {})
    except httpx.RequestError as exc:
        raise ValueError("VPN_MANAGER_UNAVAILABLE") from exc

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.is_error:
        error = data.get("error") if isinstance(data, dict) else ""
        raise ValueError(str(error or "VPN_MANAGER_ERROR"))
    if not isinstance(data, dict):
        raise ValueError("VPN_MANAGER_ERROR")
    return data

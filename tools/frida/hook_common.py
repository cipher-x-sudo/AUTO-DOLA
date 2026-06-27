"""Shared helpers for Frida lab hooks."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import frida

ROOT = Path(__file__).resolve().parent
HOOK_JS = ROOT / "hook_license.js"

DEFAULT_BACKEND = Path(
    os.environ.get(
        "NEXUS_BACKEND_DIR",
        Path.home() / "AppData/Local/Programs/Nexus Automator/resources/backend",
    )
)
DEFAULT_NEXUS_EXE = Path(
    os.environ.get(
        "NEXUS_APP_EXE",
        Path.home() / "AppData/Local/Programs/Nexus Automator/Nexus Automator.exe",
    )
)
TOKEN_DIR = Path(os.environ["APPDATA"]) / "YousMind AI"
TOKEN_FILE = TOKEN_DIR / ".api_token"
API_BASE = "http://127.0.0.1:5000"


def ensure_token() -> str:
    if TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            os.environ["NEXUS_API_TOKEN"] = token
            return token

    token = os.environ.get("NEXUS_API_TOKEN", "").strip()
    if not token:
        token = os.urandom(32).hex()
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    os.environ["NEXUS_API_TOKEN"] = token
    return token


def read_api_token() -> str:
    if TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    return ensure_token()


def hook_source() -> str:
    return HOOK_JS.read_text(encoding="utf-8")


def on_frida_message(message, _data):
    if message["type"] == "send":
        print(message["payload"])
    elif message["type"] == "error":
        print(f"[frida-error] {message.get('stack', message)}", file=sys.stderr)
    else:
        print(message)


def attach_hook(device: frida.core.Device, pid: int, source: str | None = None):
    source = source or hook_source()
    session = device.attach(pid)
    script = session.create_script(source)
    script.on("message", on_frida_message)
    script.load()
    return session, script


def force_patch(script, attempts: int = 6, delay: float = 0.4) -> dict:
    for _ in range(attempts):
        try:
            script.exports_sync.patchnow()
        except Exception:
            pass
        time.sleep(delay)
    try:
        return script.exports_sync.stats()
    except Exception:
        return {}


def request_api(path: str, token: str, method: str = "GET", body: dict | None = None, timeout: float = 6.0):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=payload,
        method=method,
        headers={"X-API-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def wait_for_health(token: str, seconds: float = 25.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            request_api("/api/health", token, timeout=1.5)
            return True
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.4)
    return False


def probe_license(token: str | None = None) -> None:
    token = token or read_api_token()
    for method, path, body in [
        ("GET", "/api/get-license-cache", None),
        ("POST", "/api/verify-license-status", {}),
    ]:
        try:
            status, data = request_api(path, token, method=method, body=body, timeout=8.0)
            print(f"{method} {path} -> {status} {json.dumps(data)}")
        except Exception as exc:
            print(f"{method} {path} -> ERROR: {exc}")

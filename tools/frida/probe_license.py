"""Quick local probe for Nexus server.exe license endpoints (lab use)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BACKEND = Path(
    os.environ.get(
        "NEXUS_BACKEND_DIR",
        Path.home()
        / "AppData/Local/Programs/Nexus Automator/resources/backend",
    )
)
TOKEN_DIR = Path(os.environ["APPDATA"]) / "YousMind AI"
TOKEN_FILE = TOKEN_DIR / ".api_token"
BASE = "http://127.0.0.1:5000"


def _request(method: str, path: str, token: str, body: dict | None = None, timeout: float = 5.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={
            "X-API-Token": token,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = os.environ.get("NEXUS_API_TOKEN") or TOKEN_FILE.read_text(encoding="utf-8").strip()
    server = DEFAULT_BACKEND / "server.exe"
    if not server.is_file():
        print(f"server.exe not found: {server}", file=sys.stderr)
        return 1

    proc = subprocess.Popen(
        [str(server)],
        cwd=str(server.parent),
        env={**os.environ, "NEXUS_API_TOKEN": token},
    )
    try:
        for _ in range(30):
            try:
                status, payload = _request("GET", "/api/health", token, timeout=1.0)
                print("health:", status, payload)
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.5)
        else:
            print("server did not become ready", file=sys.stderr)
            return 2

        for path, method, body in [
            ("/api/get-license-cache", "GET", None),
            ("/api/verify-license-status", "POST", {}),
            (
                "/api/set-license-cache",
                "POST",
                {
                    "pcId": "LAB-PC",
                    "approved": True,
                    "secureToken": "lab-local-bypass",
                },
            ),
        ]:
            try:
                status, payload = _request(method, path, token, body=body, timeout=8.0)
                print(f"{method} {path}:", status, json.dumps(payload, indent=2))
            except Exception as exc:
                print(f"{method} {path}: ERROR {exc}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


DISPLAY = os.environ.get("DISPLAY", ":99")
BASE_PROFILE_DIR = Path(os.environ.get("CHROME_PROFILE_DIR", "/data/browser-profile"))
LOG_DIR = Path("/data/logs")
PORT_START = int(os.environ.get("BROWSER_SLOT_PORT_START", "9300"))
PORT_END = int(os.environ.get("BROWSER_SLOT_PORT_END", "9399"))
EXTERNAL_PORT_START = int(os.environ.get("BROWSER_SLOT_EXTERNAL_PORT_START", "10300"))
WINDOW_WIDTH = 1365
WINDOW_HEIGHT = 900

LOCK = threading.Lock()
SLOTS: dict[str, dict] = {}


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("content-length", "0") or "0")
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode())


def free_port() -> tuple[int, int]:
    used = {slot["port"] for slot in SLOTS.values()}
    for port in range(PORT_START, PORT_END + 1):
        if port not in used:
            return port, EXTERNAL_PORT_START + (port - PORT_START)
    raise RuntimeError("No browser slots available.")


def proxy_server_arg(proxy_url: str) -> str:
    if not proxy_url:
        return ""
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{parsed.hostname}"


def proxy_credentials(proxy_url: str) -> dict[str, str]:
    parsed = urlparse(proxy_url)
    if not parsed.username or not parsed.password:
        return {}
    return {
        "username": unquote(parsed.username),
        "password": unquote(parsed.password),
    }


def browser_launch_args(profile_dir: Path, port: int, x: int, y: int, proxy_url: str = "") -> list[str]:
    args = [
        "chromium",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
        f"--window-position={x},{y}",
    ]
    proxy_arg = proxy_server_arg(proxy_url)
    if proxy_arg:
        args.append(f"--proxy-server={proxy_arg}")
    args.append("about:blank")
    return args


def wait_for_port(port: int, timeout: float = 20.0, host: str = "127.0.0.1") -> None:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"Browser CDP port {port} did not open.")


def launch_slot(proxy_url: str = "") -> dict:
    with LOCK:
        slot_number = len(SLOTS) + 1
        port, external_port = free_port()
        slot_id = f"slot-{int(time.time() * 1000)}-{port}"
        profile_dir = BASE_PROFILE_DIR / "slots" / slot_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        credentials = proxy_credentials(proxy_url)
        x = ((slot_number - 1) % 5) * 40
        y = ((slot_number - 1) % 5) * 35
        args = browser_launch_args(profile_dir, port, x, y, proxy_url)
        log_file = (LOG_DIR / f"chromium-{slot_id}.log").open("ab")
        process = subprocess.Popen(args, stdout=log_file, stderr=log_file, env={**os.environ, "DISPLAY": DISPLAY})
        try:
            wait_for_port(port)
            forward_log = (LOG_DIR / f"cdp-forward-{slot_id}.log").open("ab")
            forward_process = subprocess.Popen(
                [
                    "socat",
                    f"TCP-LISTEN:{external_port},fork,reuseaddr,bind=0.0.0.0",
                    f"TCP:127.0.0.1:{port}",
                ],
                stdout=forward_log,
                stderr=forward_log,
            )
            wait_for_port(external_port, host="127.0.0.1")
        except Exception:
            process.terminate()
            raise
        container_ip = socket.gethostbyname(socket.gethostname())
        slot = {
            "slot_id": slot_id,
            "slot_number": slot_number,
            "port": port,
            "external_port": external_port,
            "cdp_url": f"http://127.0.0.1:{port}",
            "container_cdp_url": f"http://{container_ip}:{external_port}",
            "profile_dir": str(profile_dir),
            "pid": process.pid,
            "forward_pid": forward_process.pid,
            "proxy_active": bool(proxy_url),
            "proxy_host": public_proxy_host(proxy_url),
            "proxy_auth_mode": "cdp" if credentials else ("none" if not proxy_url else "proxy-server"),
            "proxy_username": credentials.get("username", ""),
            "proxy_password": credentials.get("password", ""),
            "launch_url": "about:blank",
            "started_at": time.time(),
            "process": process,
            "forward_process": forward_process,
            "log_file": log_file,
            "forward_log": forward_log,
        }
        SLOTS[slot_id] = slot
        return connection_slot(slot)


def close_slot(slot_id: str) -> bool:
    with LOCK:
        slot = SLOTS.pop(slot_id, None)
    if not slot:
        return False
    process: subprocess.Popen = slot["process"]
    forward_process: subprocess.Popen | None = slot.get("forward_process")
    if forward_process and forward_process.poll() is None:
        forward_process.terminate()
        try:
            forward_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            forward_process.kill()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    try:
        slot["log_file"].close()
    except Exception:
        pass
    try:
        slot["forward_log"].close()
    except Exception:
        pass
    shutil.rmtree(slot["profile_dir"], ignore_errors=True)
    return True


def public_proxy_host(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return ""
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


def public_slot(slot: dict) -> dict:
    return {
        key: value
        for key, value in slot.items()
        if key not in {"process", "forward_process", "log_file", "forward_log", "proxy_username", "proxy_password"}
    }


def connection_slot(slot: dict) -> dict:
    return {
        **public_slot(slot),
        "proxy_username": slot.get("proxy_username", ""),
        "proxy_password": slot.get("proxy_password", ""),
    }


def status() -> dict:
    with LOCK:
        slots = [public_slot(slot) for slot in SLOTS.values()]
    return {
        "ok": True,
        "active_browser_count": len(slots),
        "max_browser_slots": PORT_END - PORT_START + 1,
        "active_cdp_ports": [slot["external_port"] for slot in slots],
        "slots": slots,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/status":
            json_response(self, 200, status())
            return
        json_response(self, 404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        try:
            payload = read_json(self)
            if self.path == "/launch":
                json_response(self, 200, {"ok": True, "slot": launch_slot(str(payload.get("proxy_url") or ""))})
                return
            if self.path == "/close":
                slot_id = str(payload.get("slot_id") or "")
                json_response(self, 200, {"ok": True, "closed": close_slot(slot_id)})
                return
            json_response(self, 404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return


def shutdown(*_args: object) -> None:
    for slot_id in list(SLOTS):
        close_slot(slot_id)
    raise SystemExit(0)


if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server = ThreadingHTTPServer(("0.0.0.0", 7070), Handler)
    server.serve_forever()

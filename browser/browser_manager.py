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
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


DISPLAY = os.environ.get("DISPLAY", ":99")
BASE_PROFILE_DIR = Path(os.environ.get("CHROME_PROFILE_DIR", "/data/browser-profile"))
LOG_DIR = Path("/data/logs")
VPN_DIR = Path(os.environ.get("VPN_CONFIG_DIR", "/data/profiles/vpn"))
SLOT_IMAGE = os.environ.get("BROWSER_SLOT_IMAGE", "auto-dola-browser")
SLOT_PROFILE_VOLUME = os.environ.get("BROWSER_SLOT_PROFILE_VOLUME", "")
SLOT_PROFILES_VOLUME = os.environ.get("BROWSER_SLOT_PROFILES_VOLUME", "")
SLOT_LOGS_VOLUME = os.environ.get("BROWSER_SLOT_LOGS_VOLUME", "")
SLOT_NETWORK = os.environ.get("BROWSER_SLOT_NETWORK", "")
PORT_START = int(os.environ.get("BROWSER_SLOT_PORT_START", "9300"))
PORT_END = int(os.environ.get("BROWSER_SLOT_PORT_END", "9399"))
EXTERNAL_PORT_START = int(os.environ.get("BROWSER_SLOT_EXTERNAL_PORT_START", "10300"))
WINDOW_WIDTH = 1365
WINDOW_HEIGHT = 900

LOCK = threading.Lock()
SLOTS: dict[str, dict] = {}
VPN_SLOT_CONTAINERS: dict[str, dict] = {}
VPN_STATE: dict[str, object] = {
    "connected": False,
    "process": None,
    "auth_file": "",
    "config_name": "",
    "username_masked": "",
    "ip": "",
    "connected_at": 0,
    "log_file": None,
}


def docker_available() -> bool:
    return Path("/var/run/docker.sock").exists() and shutil.which("docker") is not None


def docker_json(args: list[str]) -> object:
    output = subprocess.check_output(["docker", *args], text=True)
    return json.loads(output)


def current_container_name() -> str:
    return socket.gethostname()


def current_docker_inspect() -> dict:
    data = docker_json(["inspect", current_container_name()])
    if not isinstance(data, list) or not data:
        raise RuntimeError("Docker inspect returned no current container metadata.")
    return data[0]


def current_network_name() -> str:
    if SLOT_NETWORK:
        return SLOT_NETWORK
    inspect = current_docker_inspect()
    networks = ((inspect.get("NetworkSettings") or {}).get("Networks") or {})
    if not networks:
        raise RuntimeError("VPN_SLOT_NETWORK_MISSING")
    return next(iter(networks.keys()))


def mounted_volume_name(destination: str, fallback: str) -> str:
    if fallback:
        return fallback
    inspect = current_docker_inspect()
    for mount in inspect.get("Mounts") or []:
        if mount.get("Destination") == destination and mount.get("Type") == "volume":
            return str(mount.get("Name") or "")
    raise RuntimeError(f"VPN_SLOT_VOLUME_MISSING:{destination}")


def wait_for_manager(url: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/status", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError("VPN_SLOT_LAUNCH_FAILED")


def manager_post(url: str, endpoint: str, payload: dict, timeout: float = 120.0) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{url.rstrip('/')}{endpoint}",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode())
    if not isinstance(data, dict):
        raise RuntimeError("VPN_SLOT_MANAGER_ERROR")
    if not data.get("ok", True):
        raise RuntimeError(str(data.get("error") or "VPN_SLOT_MANAGER_ERROR"))
    return data


def launch_isolated_vpn_slot(config_path: str, config_name: str, username: str, password: str, headless: bool = False) -> dict:
    if not docker_available():
        raise RuntimeError("VPN_SLOT_DOCKER_UNAVAILABLE")
    config = validate_vpn_config_path(config_path)
    slot_id = f"vpn-slot-{int(time.time() * 1000)}"
    container_name = f"auto-dola-{slot_id}"
    child_profile_root = f"/data/browser-profile/vpn-slots/{slot_id}"
    network = current_network_name()
    profile_volume = mounted_volume_name("/data/browser-profile", SLOT_PROFILE_VOLUME)
    profiles_volume = mounted_volume_name("/data/profiles", SLOT_PROFILES_VOLUME)
    logs_volume = mounted_volume_name("/data/logs", SLOT_LOGS_VOLUME)
    subprocess.check_call(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "--cap-add",
            "NET_ADMIN",
            "--device",
            "/dev/net/tun:/dev/net/tun",
            "--network",
            network,
            "-e",
            "VPN_CONFIG_DIR=/data/profiles/vpn",
            "-e",
            f"CHROME_PROFILE_DIR={child_profile_root}",
            "-e",
            f"BROWSER_HEADLESS={'1' if headless else '0'}",
            "-v",
            f"{profile_volume}:/data/browser-profile",
            "-v",
            f"{profiles_volume}:/data/profiles",
            "-v",
            f"{logs_volume}:/data/logs",
            SLOT_IMAGE,
        ]
    )
    manager_url = f"http://{container_name}:7070"
    try:
        wait_for_manager(manager_url)
        vpn_result = manager_post(
            manager_url,
            "/vpn/connect",
            {
                "config_path": str(config),
                "config_name": config_name or config.name,
                "username": username,
                "password": password,
            },
            timeout=120,
        )
    except Exception:
        subprocess.call(["docker", "rm", "-f", container_name])
        raise
    slot = {
        "ok": True,
        "slot_id": slot_id,
        "container_name": container_name,
        "manager_url": manager_url,
        "profile_root": child_profile_root,
        "headless": headless,
        "config_name": config_name or config.name,
        "username_masked": mask_username(username),
        "ip": vpn_result.get("ip", ""),
        "started_at": time.time(),
    }
    with LOCK:
        VPN_SLOT_CONTAINERS[slot_id] = slot
    return slot


def close_isolated_vpn_slot(slot_id: str = "", container_name: str = "") -> bool:
    with LOCK:
        slot = VPN_SLOT_CONTAINERS.pop(slot_id, None) if slot_id else None
    name = container_name or str((slot or {}).get("container_name") or "")
    manager_url = str((slot or {}).get("manager_url") or (f"http://{name}:7070" if name else ""))
    if manager_url:
        try:
            manager_post(manager_url, "/vpn/disconnect", {}, timeout=15)
        except Exception:
            pass
    if name:
        subprocess.call(["docker", "rm", "-f", name])
        return True
    return False


def kill_all() -> dict:
    browser_slot_ids = list(SLOTS)
    vpn_slot_ids = list(VPN_SLOT_CONTAINERS)
    closed_browser_slots = 0
    closed_vpn_slots = 0
    for slot_id in browser_slot_ids:
        if close_slot(slot_id, delete_profile=True):
            closed_browser_slots += 1
    for slot_id in vpn_slot_ids:
        if close_isolated_vpn_slot(slot_id):
            closed_vpn_slots += 1
    vpn_disconnected = disconnect_vpn()
    return {
        "ok": True,
        "closed_browser_slots": closed_browser_slots,
        "closed_vpn_slots": closed_vpn_slots,
        "vpn_disconnected": vpn_disconnected,
    }


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


def browser_headless_enabled(value: object = None) -> bool:
    raw = os.environ.get("BROWSER_HEADLESS", "") if value is None else str(value)
    return raw.lower() in {"1", "true", "yes", "on"}


def apply_headless_args(args: list[str], headless: bool) -> list[str]:
    if not headless:
        return args
    next_args = list(args)
    next_args.insert(1, "--headless=new")
    return next_args


def tail_text(path: Path, max_chars: int = 4000) -> str:
    try:
        return path.read_text(errors="ignore")[-max_chars:]
    except Exception:
        return ""


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


def current_ip(timeout: float = 15.0) -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as response:
            return response.read().decode().strip()
    except Exception:
        return ""


def validate_vpn_config_path(config_path: str) -> Path:
    path = Path(config_path).resolve()
    base = VPN_DIR.resolve()
    if base not in path.parents or path.suffix.lower() != ".ovpn":
        raise RuntimeError("VPN config path is outside VPN config directory.")
    if not path.exists():
        raise RuntimeError("VPN_CONFIG_MISSING")
    return path


def mask_username(username: str) -> str:
    if len(username) <= 3:
        return "***"
    return f"{username[:2]}***{username[-1:]}"


def vpn_status() -> dict:
    process: subprocess.Popen | None = VPN_STATE.get("process")  # type: ignore[assignment]
    connected = bool(process and process.poll() is None and VPN_STATE.get("connected"))
    return {
        "ok": True,
        "connected": connected,
        "config_name": VPN_STATE.get("config_name") if connected else "",
        "username_masked": VPN_STATE.get("username_masked") if connected else "",
        "ip": VPN_STATE.get("ip") if connected else "",
        "connected_at": VPN_STATE.get("connected_at") if connected else 0,
    }


def disconnect_vpn() -> bool:
    process: subprocess.Popen | None = VPN_STATE.get("process")  # type: ignore[assignment]
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    auth_file = str(VPN_STATE.get("auth_file") or "")
    if auth_file:
        Path(auth_file).unlink(missing_ok=True)
    log_file = VPN_STATE.get("log_file")
    try:
        if log_file:
            log_file.close()  # type: ignore[attr-defined]
    except Exception:
        pass
    VPN_STATE.update(
        {
            "connected": False,
            "process": None,
            "auth_file": "",
            "config_name": "",
            "username_masked": "",
            "ip": "",
            "connected_at": 0,
            "log_file": None,
        }
    )
    return True


def connect_vpn(config_path: str, config_name: str, username: str, password: str, timeout: float = 90.0) -> dict:
    if not Path("/dev/net/tun").exists():
        raise RuntimeError("VPN_NO_TUN_DEVICE")
    if not username or not password:
        raise RuntimeError("VPN_AUTH_FAILED")
    disconnect_vpn()
    config = validate_vpn_config_path(config_path)
    before_ip = current_ip(timeout=10)
    auth_handle = tempfile.NamedTemporaryFile("w", delete=False, prefix="openvpn-auth-", dir="/tmp", encoding="utf-8")
    auth_handle.write(f"{username}\n{password}\n")
    auth_handle.close()
    log_path = LOG_DIR / f"openvpn-{int(time.time() * 1000)}.log"
    log_file = log_path.open("ab")
    process = subprocess.Popen(
        ["openvpn", "--config", str(config), "--auth-user-pass", auth_handle.name, "--verb", "3"],
        stdout=log_file,
        stderr=log_file,
        env=os.environ.copy(),
    )
    VPN_STATE.update(
        {
            "connected": False,
            "process": process,
            "auth_file": auth_handle.name,
            "config_name": config_name or config.name,
            "username_masked": mask_username(username),
            "ip": "",
            "connected_at": 0,
            "log_file": log_file,
        }
    )
    deadline = time.monotonic() + timeout
    last_log = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            disconnect_vpn()
            raise RuntimeError("VPN_AUTH_FAILED")
        try:
            last_log = log_path.read_text(errors="ignore")[-4000:]
        except Exception:
            last_log = ""
        if "Initialization Sequence Completed" in last_log:
            after_ip = current_ip(timeout=15)
            VPN_STATE.update({"connected": True, "ip": after_ip, "connected_at": time.time()})
            return {
                "ok": True,
                "connected": True,
                "config_name": config_name or config.name,
                "username_masked": mask_username(username),
                "ip_before": before_ip,
                "ip": after_ip,
            }
        if "AUTH_FAILED" in last_log or "auth-failure" in last_log.lower():
            disconnect_vpn()
            raise RuntimeError("VPN_AUTH_FAILED")
        time.sleep(1)
    disconnect_vpn()
    raise RuntimeError("VPN_CONNECT_TIMEOUT")


def validate_profile_dir(profile_dir: str) -> Path:
    path = Path(profile_dir).resolve()
    base = (BASE_PROFILE_DIR / "slots").resolve()
    if base not in path.parents and path != base:
        raise RuntimeError("Profile path is outside browser profile slots directory.")
    return path


def launch_slot(proxy_url: str = "", profile_dir: str = "", headless: bool | None = None) -> dict:
    with LOCK:
        slot_number = len(SLOTS) + 1
        port, external_port = free_port()
        slot_id = f"slot-{int(time.time() * 1000)}-{port}"
        profile_path = validate_profile_dir(profile_dir) if profile_dir else BASE_PROFILE_DIR / "slots" / slot_id
        profile_path.mkdir(parents=True, exist_ok=True)
        credentials = proxy_credentials(proxy_url)
        x = ((slot_number - 1) % 5) * 40
        y = ((slot_number - 1) % 5) * 35
        headless_enabled = browser_headless_enabled() if headless is None else headless
        args = apply_headless_args(browser_launch_args(profile_path, port, x, y, proxy_url), headless_enabled)
        log_path = LOG_DIR / f"chromium-{slot_id}.log"
        log_file = log_path.open("ab")
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
        except Exception as exc:
            process.terminate()
            try:
                process.wait(timeout=3)
            except Exception:
                process.kill()
            try:
                log_file.close()
            except Exception:
                pass
            raise RuntimeError(
                json.dumps(
                    {
                        "error": "CHROMIUM_LAUNCH_FAILED",
                        "detail": str(exc),
                        "slot_id": slot_id,
                        "profile_dir": str(profile_path),
                        "log_file": str(log_path),
                        "log_snippet": tail_text(log_path),
                    }
                )
            ) from exc
        container_ip = socket.gethostbyname(socket.gethostname())
        slot = {
            "slot_id": slot_id,
            "slot_number": slot_number,
            "port": port,
            "external_port": external_port,
            "cdp_url": f"http://127.0.0.1:{port}",
            "container_cdp_url": f"http://{container_ip}:{external_port}",
            "profile_dir": str(profile_path),
            "pid": process.pid,
            "forward_pid": forward_process.pid,
            "proxy_active": bool(proxy_url),
            "proxy_host": public_proxy_host(proxy_url),
            "proxy_auth_mode": "cdp" if credentials else ("none" if not proxy_url else "proxy-server"),
            "proxy_username": credentials.get("username", ""),
            "proxy_password": credentials.get("password", ""),
            "launch_url": "about:blank",
            "headless": headless_enabled,
            "started_at": time.time(),
            "process": process,
            "forward_process": forward_process,
            "log_file": log_file,
            "log_path": str(log_path),
            "forward_log": forward_log,
        }
        SLOTS[slot_id] = slot
        return connection_slot(slot)


def close_slot(slot_id: str, delete_profile: bool = True) -> bool:
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
    if delete_profile:
        shutil.rmtree(slot["profile_dir"], ignore_errors=True)
    return True


def delete_profile(profile_dir: str) -> bool:
    path = validate_profile_dir(profile_dir)
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


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
        vpn_slots = [dict(slot) for slot in VPN_SLOT_CONTAINERS.values()]
    return {
        "ok": True,
        "active_browser_count": len(slots),
        "max_browser_slots": PORT_END - PORT_START + 1,
        "active_cdp_ports": [slot["external_port"] for slot in slots],
        "slots": slots,
        "active_vpn_browser_count": len(vpn_slots),
        "vpn_slots": vpn_slots,
        "browser_headless": browser_headless_enabled(),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/status":
            json_response(self, 200, status())
            return
        if self.path == "/vpn/status":
            json_response(self, 200, vpn_status())
            return
        json_response(self, 404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        try:
            payload = read_json(self)
            if self.path == "/launch":
                try:
                    slot = launch_slot(
                        str(payload.get("proxy_url") or ""),
                        str(payload.get("profile_dir") or ""),
                        bool(payload.get("headless", browser_headless_enabled())),
                    )
                except RuntimeError as exc:
                    try:
                        error_payload = json.loads(str(exc))
                    except Exception:
                        error_payload = {"error": "BROWSER_LAUNCH_FAILED", "detail": str(exc)}
                    json_response(self, 500, {"ok": False, **error_payload})
                    return
                json_response(
                    self,
                    200,
                    {"ok": True, "slot": slot},
                )
                return
            if self.path == "/close":
                slot_id = str(payload.get("slot_id") or "")
                delete = bool(payload.get("delete_profile", True))
                json_response(self, 200, {"ok": True, "closed": close_slot(slot_id, delete_profile=delete)})
                return
            if self.path == "/delete-profile":
                profile_dir = str(payload.get("profile_dir") or "")
                json_response(self, 200, {"ok": True, "deleted": delete_profile(profile_dir)})
                return
            if self.path == "/vpn/connect":
                result = connect_vpn(
                    str(payload.get("config_path") or ""),
                    str(payload.get("config_name") or ""),
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                    bool(payload.get("headless", browser_headless_enabled())),
                )
                json_response(self, 200, result)
                return
            if self.path == "/vpn/disconnect":
                json_response(self, 200, {"ok": True, "disconnected": disconnect_vpn()})
                return
            if self.path == "/vpn/status":
                json_response(self, 200, vpn_status())
                return
            if self.path == "/vpn/test-ip":
                result = connect_vpn(
                    str(payload.get("config_path") or ""),
                    str(payload.get("config_name") or ""),
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                )
                try:
                    json_response(self, 200, result)
                finally:
                    disconnect_vpn()
                return
            if self.path == "/vpn-slot/launch":
                result = launch_isolated_vpn_slot(
                    str(payload.get("config_path") or ""),
                    str(payload.get("config_name") or ""),
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                )
                json_response(self, 200, result)
                return
            if self.path == "/vpn-slot/close":
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "closed": close_isolated_vpn_slot(
                            str(payload.get("slot_id") or ""),
                            str(payload.get("container_name") or ""),
                        ),
                    },
                )
                return
            if self.path == "/kill-all":
                json_response(self, 200, kill_all())
                return
            json_response(self, 404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return


def shutdown(*_args: object) -> None:
    kill_all()
    raise SystemExit(0)


if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server = ThreadingHTTPServer(("0.0.0.0", 7070), Handler)
    server.serve_forever()

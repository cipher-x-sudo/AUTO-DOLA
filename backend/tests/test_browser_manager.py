from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_browser_manager():
    module_path = Path(__file__).resolve().parents[2] / "browser" / "browser_manager.py"
    spec = importlib.util.spec_from_file_location("browser_manager_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


class FakeLog:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_close_slot_removes_profile_and_processes(tmp_path: Path) -> None:
    manager = load_browser_manager()
    manager.SLOTS.clear()
    profile_dir = tmp_path / "slots" / "slot-test"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    process = FakeProcess()
    forward_process = FakeProcess()
    log_file = FakeLog()
    forward_log = FakeLog()
    manager.SLOTS["slot-test"] = {
        "slot_id": "slot-test",
        "process": process,
        "forward_process": forward_process,
        "log_file": log_file,
        "forward_log": forward_log,
        "profile_dir": str(profile_dir),
    }

    assert manager.close_slot("slot-test") is True

    assert "slot-test" not in manager.SLOTS
    assert not profile_dir.exists()
    assert process.terminated is True
    assert forward_process.terminated is True
    assert log_file.closed is True
    assert forward_log.closed is True


def test_close_slot_can_keep_profile(tmp_path: Path) -> None:
    manager = load_browser_manager()
    manager.SLOTS.clear()
    profile_dir = tmp_path / "slots" / "slot-test"
    profile_dir.mkdir(parents=True)
    process = FakeProcess()
    forward_process = FakeProcess()
    manager.SLOTS["slot-test"] = {
        "slot_id": "slot-test",
        "process": process,
        "forward_process": forward_process,
        "log_file": FakeLog(),
        "forward_log": FakeLog(),
        "profile_dir": str(profile_dir),
    }

    assert manager.close_slot("slot-test", delete_profile=False) is True

    assert profile_dir.exists()
    assert process.terminated is True
    assert forward_process.terminated is True


def test_close_slot_returns_false_for_missing_slot() -> None:
    manager = load_browser_manager()
    manager.SLOTS.clear()

    assert manager.close_slot("missing-slot") is False


def test_delete_profile_removes_valid_retained_profile(tmp_path: Path) -> None:
    manager = load_browser_manager()
    manager.BASE_PROFILE_DIR = tmp_path
    profile_dir = tmp_path / "slots" / "slot-test"
    profile_dir.mkdir(parents=True)

    assert manager.delete_profile(str(profile_dir)) is True
    assert not profile_dir.exists()


def test_authenticated_proxy_uses_proxy_server_and_cdp_credentials() -> None:
    manager = load_browser_manager()

    proxy_url = "http://user:pa%40ss@proxy.example.com:2312"

    assert manager.proxy_server_arg(proxy_url) == "http://proxy.example.com:2312"
    assert manager.proxy_credentials(proxy_url) == {"username": "user", "password": "pa@ss"}


def test_browser_launch_args_use_one_blank_tab_and_no_extension(tmp_path: Path) -> None:
    manager = load_browser_manager()

    args = manager.browser_launch_args(
        tmp_path / "profile",
        9300,
        0,
        0,
        "http://user:secret@proxy.example.com:2312",
    )

    assert args[-1] == "about:blank"
    assert "--proxy-server=http://proxy.example.com:2312" in args
    assert not any(argument.startswith("--load-extension=") for argument in args)
    assert not any("dola.com" in argument for argument in args)


def test_apply_headless_args_adds_chrome_headless_flag(tmp_path: Path) -> None:
    manager = load_browser_manager()
    args = manager.browser_launch_args(tmp_path / "profile", 9300, 0, 0)

    headless_args = manager.apply_headless_args(args, True)

    assert "--headless=new" in headless_args
    assert headless_args[-1] == "about:blank"


def test_proxy_credentials_skip_non_auth_or_empty_proxy() -> None:
    manager = load_browser_manager()

    assert manager.proxy_credentials("") == {}
    assert manager.proxy_credentials("http://proxy.example.com:2312") == {}


def test_public_slot_redacts_proxy_credentials() -> None:
    manager = load_browser_manager()

    public = manager.public_slot(
        {
            "slot_id": "slot-1",
            "proxy_active": True,
            "proxy_host": "proxy.example.com:2312",
            "proxy_auth_mode": "cdp",
            "proxy_username": "user",
            "proxy_password": "secret",
            "process": object(),
        }
    )

    assert public["proxy_auth_mode"] == "cdp"
    assert public["proxy_host"] == "proxy.example.com:2312"
    assert "proxy_username" not in public
    assert "proxy_password" not in public


def test_connection_slot_keeps_credentials_for_internal_cdp_handoff() -> None:
    manager = load_browser_manager()

    connection = manager.connection_slot(
        {
            "slot_id": "slot-1",
            "proxy_auth_mode": "cdp",
            "proxy_username": "user",
            "proxy_password": "secret",
            "process": object(),
        }
    )

    assert connection["proxy_username"] == "user"
    assert connection["proxy_password"] == "secret"
    assert "process" not in connection


def test_validate_vpn_config_path_rejects_outside_path(tmp_path: Path) -> None:
    manager = load_browser_manager()
    manager.VPN_DIR = tmp_path / "vpn"
    manager.VPN_DIR.mkdir()
    outside = tmp_path / "outside.ovpn"
    outside.write_text("client", encoding="utf-8")

    try:
        manager.validate_vpn_config_path(str(outside))
    except RuntimeError as exc:
        assert "outside VPN config directory" in str(exc)
    else:
        raise AssertionError("outside VPN path accepted")


def test_validate_vpn_config_path_accepts_ovpn_under_vpn_dir(tmp_path: Path) -> None:
    manager = load_browser_manager()
    manager.VPN_DIR = tmp_path / "vpn"
    manager.VPN_DIR.mkdir()
    config = manager.VPN_DIR / "hk.ovpn"
    config.write_text("client", encoding="utf-8")

    assert manager.validate_vpn_config_path(str(config)) == config.resolve()


def test_isolated_vpn_slot_uses_unique_child_profile_root(monkeypatch, tmp_path: Path) -> None:
    manager = load_browser_manager()
    manager.VPN_SLOT_CONTAINERS.clear()
    manager.VPN_DIR = tmp_path / "vpn"
    manager.VPN_DIR.mkdir()
    config = manager.VPN_DIR / "hk.ovpn"
    config.write_text("client", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(manager, "docker_available", lambda: True)
    monkeypatch.setattr(manager, "current_network_name", lambda: "auto-dola_default")
    monkeypatch.setattr(manager, "mounted_volume_name", lambda destination, _fallback: f"volume-{destination.rsplit('/', 1)[-1]}")
    monkeypatch.setattr(manager, "wait_for_manager", lambda _url: None)
    monkeypatch.setattr(manager, "manager_post", lambda *_args, **_kwargs: {"ok": True, "ip": "1.2.3.4"})

    def fake_run(command: list[str], **_kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(manager.subprocess, "run", fake_run)

    slot = manager.launch_isolated_vpn_slot(str(config), config.name, "user", "pass", headless=True)
    command = commands[0]
    profile_env = next(value for value in command if value.startswith("CHROME_PROFILE_DIR="))

    assert slot["profile_root"].startswith("/data/browser-profile/vpn-slots/vpn-slot-")
    assert profile_env == f"CHROME_PROFILE_DIR={slot['profile_root']}"
    assert "BROWSER_HEADLESS=1" in command


def test_isolated_vpn_slot_preserves_docker_stderr(monkeypatch, tmp_path: Path) -> None:
    manager = load_browser_manager()
    manager.VPN_DIR = tmp_path / "vpn"
    manager.VPN_DIR.mkdir()
    manager.VPN_SLOT_LOG_ROOT = tmp_path / "logs"
    config = manager.VPN_DIR / "hk.ovpn"
    config.write_text("client", encoding="utf-8")
    monkeypatch.setattr(manager, "docker_available", lambda: True)
    monkeypatch.setattr(manager, "current_network_name", lambda: "missing-network")
    monkeypatch.setattr(manager, "mounted_volume_name", lambda *_args: "volume")
    monkeypatch.setattr(
        manager.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 125, "stdout": "", "stderr": "network missing-network not found"})(),
    )

    with pytest.raises(manager.VpnSlotLaunchError, match="VPN_SLOT_CONTAINER_LAUNCH_FAILED") as exc_info:
        manager.launch_isolated_vpn_slot(str(config), config.name, "user", "pass")

    diagnostic = manager.VPN_SLOT_LOG_ROOT / exc_info.value.slot_id / "diagnostic.json"
    assert "network missing-network not found" in diagnostic.read_text(encoding="utf-8")


def test_kill_all_closes_browser_slots_vpn_slots_and_vpn(monkeypatch) -> None:
    manager = load_browser_manager()
    manager.SLOTS.clear()
    manager.VPN_SLOT_CONTAINERS.clear()
    manager.SLOTS["slot-1"] = {"slot_id": "slot-1"}
    manager.VPN_SLOT_CONTAINERS["vpn-slot-1"] = {"slot_id": "vpn-slot-1"}
    closed_browser: list[str] = []
    closed_vpn: list[str] = []

    monkeypatch.setattr(manager, "close_slot", lambda slot_id, delete_profile=True: closed_browser.append(slot_id) or True)
    monkeypatch.setattr(manager, "close_isolated_vpn_slot", lambda slot_id="", container_name="": closed_vpn.append(slot_id) or True)
    monkeypatch.setattr(manager, "disconnect_vpn", lambda: True)

    result = manager.kill_all()

    assert result["closed_browser_slots"] == 1
    assert result["closed_vpn_slots"] == 1
    assert result["vpn_disconnected"] is True
    assert closed_browser == ["slot-1"]
    assert closed_vpn == ["vpn-slot-1"]

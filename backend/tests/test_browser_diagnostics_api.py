from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import jobs, system


def test_browser_screenshot_serves_file_from_log_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    screenshot_dir = tmp_path / "dola-browser"
    screenshot_dir.mkdir()
    screenshot = screenshot_dir / "page-load-timeout-1.png"
    screenshot.write_bytes(b"png")
    monkeypatch.setattr(jobs.settings, "log_dir", tmp_path)

    response = jobs.browser_screenshot(screenshot.name)

    assert Path(response.path) == screenshot
    assert response.media_type == "image/png"


@pytest.mark.parametrize("filename", ["../secret.png", "not-an-image.txt", "folder/screen.png"])
def test_browser_screenshot_rejects_unsafe_paths(filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs.settings, "log_dir", tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        jobs.browser_screenshot(filename)

    assert exc_info.value.status_code == 404


def test_vpn_slot_diagnostics_and_logs_are_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    slot_id = f"vpn-slot-{'a' * 32}"
    slot_dir = tmp_path / "vpn-slots" / slot_id
    slot_dir.mkdir(parents=True)
    (slot_dir / "diagnostic.json").write_text('{"error":"VPN_AUTH_FAILED"}', encoding="utf-8")
    (slot_dir / "openvpn.log").write_text("AUTH_FAILED", encoding="utf-8")
    monkeypatch.setattr(system.settings, "log_dir", tmp_path)

    assert system.get_vpn_slot_diagnostics(slot_id)["error"] == "VPN_AUTH_FAILED"
    assert Path(system.get_vpn_slot_log(slot_id, "openvpn.log").path) == slot_dir / "openvpn.log"


@pytest.mark.parametrize("slot_id,log_name", [("../secret", "docker.log"), (f"vpn-slot-{'a' * 32}", "../secret")])
def test_vpn_slot_log_rejects_path_traversal(slot_id: str, log_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system.settings, "log_dir", tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        system.get_vpn_slot_log(slot_id, log_name)

    assert exc_info.value.status_code == 400

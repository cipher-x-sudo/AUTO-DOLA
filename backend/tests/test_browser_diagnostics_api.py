from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import jobs


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

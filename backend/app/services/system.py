from __future__ import annotations

import shutil
from pathlib import Path

from app.config import settings


def ffmpeg_status() -> dict:
    found = shutil.which(settings.ffmpeg_bin) or (settings.ffmpeg_bin if Path(settings.ffmpeg_bin).exists() else "")
    return {"available": bool(found), "path": found or settings.ffmpeg_bin}


def chrome_status() -> dict:
    candidates = [shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome")]
    win = [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    found = next((c for c in candidates if c), None)
    if not found:
        found = next((c for c in win if Path(c).exists()), "")
    return {"available": bool(found), "path": found or ""}

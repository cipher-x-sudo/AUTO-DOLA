from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from app.config import settings


def safe_filename(stem: str, suffix: str = ".mp4") -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    cleaned = "".join(ch if ch in allowed else "-" for ch in stem.strip())[:80].strip("-")
    return f"{cleaned or 'video'}{suffix}"


def ffmpeg_available() -> bool:
    return shutil.which(settings.ffmpeg_bin) is not None or Path(settings.ffmpeg_bin).exists()


def clean_video(input_path: Path, output_path: Path) -> bool:
    capture = cv2.VideoCapture(str(input_path))
    if not capture.isOpened():
        return False
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    box = detect_watermark_box(capture, width, height, min(30, total))
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    if not box:
        box = (int(width * 0.8), int(height * 0.92), int(width * 0.18), int(height * 0.05))
    x, y, w, h = expand_box(box, width, height, padding=10)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.rectangle(mask, (x, y), (x + w, y + h), 255, -1)
    temp = input_path.with_suffix(input_path.suffix + ".temp.mp4")
    writer = cv2.VideoWriter(str(temp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        writer.write(cv2.inpaint(frame, mask, 3, cv2.INPAINT_TELEA))
    capture.release()
    writer.release()
    if ffmpeg_available():
        try:
            subprocess.run([settings.ffmpeg_bin, "-y", "-i", str(temp), "-i", str(input_path), "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "18", "-c:a", "copy", str(output_path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            temp.unlink(missing_ok=True)
            return output_path.exists()
        except Exception:
            output_path.unlink(missing_ok=True)
    temp.replace(output_path)
    return output_path.exists()


def detect_watermark_box(capture, width: int, height: int, frames: int) -> tuple[int, int, int, int] | None:
    best = None
    best_area = 0
    for _ in range(frames):
        ok, frame = capture.read()
        if not ok:
            break
        x0, y0 = int(width * 0.7), int(height * 0.85)
        roi = frame[y0:height, x0:width]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, threshold = cv2.threshold(gray, 220, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
        dilated = cv2.dilate(threshold, kernel, iterations=1)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            ratio = float(w) / max(h, 1)
            if 2.2 < ratio < 6.0 and width * 0.04 < w < width * 0.25 and height * 0.01 < h < height * 0.07:
                area = w * h
                if area > best_area:
                    best_area = area
                    best = (x + x0, y + y0, w, h)
    return best


def expand_box(box: tuple[int, int, int, int], width: int, height: int, padding: int) -> tuple[int, int, int, int]:
    x, y, w, h = box
    x = max(0, x - padding)
    y = max(0, y - padding)
    return x, y, min(width - x, w + 2 * padding), min(height - y, h + 2 * padding)

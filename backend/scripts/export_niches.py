from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx


DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/1qM3UtclJIfCZxyIm-jUYRWzmRIBM92opCgEs9_xfsuM/export?format=csv&gid=0"
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "niches"


@dataclass
class ExportResult:
    total_rows: int = 0
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    saved_files: list[Path] | None = None
    messages: list[str] | None = None

    def __post_init__(self) -> None:
        self.saved_files = self.saved_files or []
        self.messages = self.messages or []


def sanitize_filename(value: str, fallback: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    cleaned = cleaned.strip("-")[:90].strip("-")
    return cleaned or fallback


def versioned_path(directory: Path, stem: str, suffix: str = ".txt") -> Path:
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def extract_google_doc_id(link: str) -> str | None:
    parsed = urlparse(link.strip())
    match = re.search(r"/document/d/([^/]+)", parsed.path)
    return match.group(1) if match else None


def normalize_headers(headers: Iterable[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for header in headers:
        key = header.strip().upper()
        if key and key not in normalized:
            normalized[key] = header
    return normalized


def parse_sheet_rows(csv_text: str) -> list[dict[str, str]]:
    text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        return []
    headers = normalize_headers(reader.fieldnames)
    name_key = headers.get("PROMPT NAME")
    link_key = headers.get("LINK")
    if not name_key or not link_key:
        raise ValueError("Sheet CSV must contain PROMPT NAME and LINK columns.")
    rows: list[dict[str, str]] = []
    for row in reader:
        rows.append(
            {
                "name": (row.get(name_key) or "").strip(),
                "link": (row.get(link_key) or "").strip(),
            }
        )
    return rows


def fetch_text(client: httpx.Client, url: str) -> str:
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()
    return response.text


def fetch_google_doc_text(client: httpx.Client, doc_id: str) -> str:
    return fetch_text(client, f"https://docs.google.com/document/d/{doc_id}/export?format=txt").lstrip("\ufeff")


def export_niches(sheet_url: str = DEFAULT_SHEET_URL, out_dir: Path = DEFAULT_OUT_DIR) -> ExportResult:
    result = ExportResult()
    out_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60) as client:
        rows = parse_sheet_rows(fetch_text(client, sheet_url))
        result.total_rows = len(rows)
        for index, row in enumerate(rows, start=1):
            name = row["name"]
            link = row["link"]
            if not link:
                result.skipped += 1
                result.messages.append(f"SKIP row {index}: missing LINK")
                continue
            doc_id = extract_google_doc_id(link)
            if not doc_id:
                result.skipped += 1
                result.messages.append(f"SKIP row {index}: LINK is not a Google Doc URL")
                continue
            stem = sanitize_filename(name, f"niche-{index}")
            path = versioned_path(out_dir, stem)
            try:
                body = fetch_google_doc_text(client, doc_id)
                path.write_text(body, encoding="utf-8")
                result.saved += 1
                result.saved_files.append(path)
                result.messages.append(f"SAVED row {index}: {path.name}")
            except Exception as exc:
                result.failed += 1
                result.messages.append(f"FAIL row {index} ({name or doc_id}): {exc}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Google Sheet niche prompt docs to local TXT files.")
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_URL, help="Public Google Sheet CSV export URL.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="Output folder for niche TXT files.")
    args = parser.parse_args()

    result = export_niches(args.sheet_url, args.out)
    for message in result.messages:
        print(message)
    print()
    print(f"Rows: {result.total_rows}")
    print(f"Saved: {result.saved}")
    print(f"Skipped: {result.skipped}")
    print(f"Failed: {result.failed}")
    print(f"Output: {args.out.resolve()}")
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())

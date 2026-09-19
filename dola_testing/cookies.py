from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


REQUIRED_FACEBOOK_COOKIES = ("c_user", "xs")
RECOMMENDED_FACEBOOK_COOKIES = ("datr", "fr", "sb")
DOLA_AUTH_FRAGMENTS = ("sessionid", "sid_guard", "sid_tt", "uid_tt")
PUBLIC_DOLA_COOKIE_NAMES = {
    "ttwid",
    "s_v_web_id",
    "hook_slardar_session_id",
    "biz_trace_id",
    "flow_user_country",
    "store-idc",
    "store-country-code",
    "store-country-code-src",
    "i18next",
    "dbx-web-theme",
    "conversation_list_v2_group_mode",
    "_ga",
    "_ga_5mr93b9jt5",
    "_gcl_au",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "passport_csrf_token_wap_state",
    "reg-store-region",
    "odin_tt",
}


def parse_cookie_header(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        if cleaned.lower().startswith("cookie:"):
            cleaned = cleaned.split(":", 1)[1].strip()
        for part in cleaned.split(";"):
            piece = part.strip()
            if not piece or "=" not in piece:
                continue
            name, value = piece.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name and value:
                cookies[name] = value
    return cookies


def load_facebook_cookie_rows(raw: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        parsed = parse_cookie_header(cleaned)
        if parsed:
            rows.append(parsed)
    if not rows:
        parsed = parse_cookie_header(raw)
        if parsed:
            rows.append(parsed)
    return rows


def missing_facebook_cookies(cookies: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED_FACEBOOK_COOKIES if not cookies.get(name)]


def facebook_c_user(cookies: dict[str, str]) -> str:
    return str(cookies.get("c_user") or "").strip()


def cookies_from_jar(jar: Any, *, domain_contains: str = "dola.com") -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    inner = getattr(jar, "jar", jar)
    try:
        iterator = list(inner)
    except TypeError:
        iterator = []
        for name, value in dict(jar).items():
            raw.append({"name": str(name), "value": str(value), "domain": f".{domain_contains}", "path": "/"})
    for cookie in iterator:
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        domain = str(getattr(cookie, "domain", "") or "")
        if not name or not value:
            continue
        rest = getattr(cookie, "_rest", None) or getattr(cookie, "rest", None) or {}
        raw.append(
            {
                "name": name,
                "value": value,
                "domain": domain or f".{domain_contains}",
                "path": str(getattr(cookie, "path", None) or "/"),
                "secure": bool(getattr(cookie, "secure", False)),
                "httpOnly": bool(rest.get("HttpOnly") or rest.get("httponly")),
            }
        )
    needle = domain_contains.lower().lstrip(".")
    scoped = [item for item in raw if needle in str(item.get("domain") or "").lower().lstrip(".")]
    if needle in {"dola.com", "www.dola.com"}:
        return filter_dola_cookies(scoped or raw)
    return scoped


def _domain_is_dola(domain: str) -> bool:
    normalized = domain.lower().strip().lstrip(".")
    return normalized in {"dola.com", "www.dola.com"} or normalized.endswith(".dola.com")


def is_dola_auth_cookie(name: str) -> bool:
    lower = name.lower()
    if lower in PUBLIC_DOLA_COOKIE_NAMES:
        return False
    return any(fragment in lower for fragment in DOLA_AUTH_FRAGMENTS)


def filter_dola_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "www.dola.com")
        if not name or not value or name in seen or not _domain_is_dola(domain):
            continue
        seen.add(name)
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": ".dola.com" if domain.lower().strip(".") in {"dola.com", "www.dola.com"} else domain,
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure", True)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
        }
        if cookie.get("expires"):
            item["expires"] = cookie["expires"]
        if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
            item["sameSite"] = cookie["sameSite"]
        filtered.append(item)
    return filtered


def cookie_header(cookies: list[dict[str, Any]] | dict[str, str]) -> str:
    if isinstance(cookies, dict):
        items = cookies.items()
    else:
        items = ((str(item.get("name") or ""), str(item.get("value") or "")) for item in cookies)
    return "; ".join(f"{name}={value}" for name, value in items if name and value)


def cookie_names(cookies: list[dict[str, Any]]) -> list[str]:
    return sorted(str(item.get("name") or "") for item in cookies if item.get("name"))


def has_dola_auth(cookies: list[dict[str, Any]] | dict[str, str]) -> bool:
    names = cookies.keys() if isinstance(cookies, dict) else (str(item.get("name") or "") for item in cookies)
    return any(is_dola_auth_cookie(name) for name in names)


def utc_stamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.strftime("%Y%m%d_%H%M%S")


def safe_stem(value: str, fallback: str = "dola") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())[:40].strip("_")
    return cleaned or fallback


LEDGER_COLUMNS = [
    "timestamp",
    "facebook_c_user",
    "status",
    "dola_auth",
    "cookie_names",
    "cookie_header",
    "json_path",
    "error",
]


def write_profile_files(
    output_dir: Path,
    *,
    facebook_c_user_id: str,
    cookies: list[dict[str, Any]],
    stamp: str | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_stem(facebook_c_user_id)}_{stamp or utc_stamp()}"
    json_path = output_dir / f"{stem}.json"
    header_path = output_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    header_path.write_text(cookie_header(cookies), encoding="utf-8")
    return {"json": json_path, "txt": header_path}


def append_ledger_row(output_dir: Path, row: dict[str, str]) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "dola_cookies.csv"
    xls_path = output_dir / "dola_cookies.xls"
    existing: list[dict[str, str]] = []
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            existing = [{key: item.get(key) or "" for key in LEDGER_COLUMNS} for item in csv.DictReader(handle)]
    existing.append({key: str(row.get(key) or "") for key in LEDGER_COLUMNS})
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_COLUMNS)
        writer.writeheader()
        writer.writerows(existing)
    _write_excel_xml(xls_path, existing)
    return {"csv": csv_path, "xls": xls_path}


def _write_excel_xml(path: Path, rows: list[dict[str, str]]) -> None:
    lines = [
        '<?xml version="1.0"?>',
        '<?mso-application progid="Excel.Sheet"?>',
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
        ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">',
        '<Worksheet ss:Name="Dola Cookies"><Table>',
        "<Row>" + "".join(f'<Cell><Data ss:Type="String">{escape(column)}</Data></Cell>' for column in LEDGER_COLUMNS) + "</Row>",
    ]
    for row in rows:
        cells = "".join(f'<Cell><Data ss:Type="String">{escape(str(row.get(column) or ""))}</Data></Cell>' for column in LEDGER_COLUMNS)
        lines.append(f"<Row>{cells}</Row>")
    lines.append("</Table></Worksheet></Workbook>")
    path.write_text("\n".join(lines), encoding="utf-8")

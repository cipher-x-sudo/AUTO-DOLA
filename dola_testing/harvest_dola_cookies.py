"""Harvest Dola cookies from Facebook cookies over pure HTTP (curl_cffi).

Put one Facebook cookie header per line in input/fb_cookies.txt, then run:

    python harvest_dola_cookies.py
    python harvest_dola_cookies.py --workers 3 --proxy-file proxies.example.txt --debug
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import secrets
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import requests as curl_requests

from bulk_export import build_bulk_payload, profile_entry, write_bulk_import, write_uid_profile
from cookies import (
    RECOMMENDED_FACEBOOK_COOKIES,
    append_ledger_row,
    cookie_header,
    cookie_names,
    cookies_from_jar,
    facebook_c_user,
    filter_dola_cookies,
    has_dola_auth,
    load_facebook_cookie_rows,
    missing_facebook_cookies,
    utc_stamp,
    write_profile_files,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "input" / "fb_cookies.txt"
DEFAULT_OUTPUT = ROOT / "output"
DOLA_HOME = "https://www.dola.com/chat/"
DOLA_AID = "495671"
FACEBOOK_APP_ID = "1819912395072617"
FACEBOOK_XD_ARBITER = "https://www.facebook.com/x/connect/xd_arbiter/?version=46"
DOLA_FB_PLATFORM_APP_ID = "2204"
GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
SAHARA_DOC_IDS = {
    "interactions": ("9911359042258025", "useSaharaCometConsentPromptInteractionsServerMutation"),
    "validation": ("27704027355919565", "useSaharaCometConsentPromptValidationServerMutation"),
    "outcome": ("9822638027828705", "useSaharaCometConsentPostPromptOutcomeServerMutation"),
}
# Match the working Cookie Chrome Open fingerprint (Desktop Chrome 153).
CHROME_MAJOR = 153
CHROME_UA = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
# curl_cffi has no chrome153 yet. Prefer chrome145: chrome146 TLS often breaks
# through residential HTTP proxies (curl 35 OPENSSL_internal) on dola.com/google.
CURL_IMPERSONATE_CANDIDATES = (
    "chrome145",
    "chrome146",
    "chrome142",
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
)


@dataclass(frozen=True)
class HarvestContext:
    proxy: str = ""
    user_agent: str = ""
    debug: bool = False
    log_prefix: str = ""


def desktop_ua_for_c_user(c_user: str) -> str:
    # Keep one sticky Desktop Chrome UA (same as working cookie tool).
    _ = c_user
    return CHROME_UA


def chrome_client_hint_headers(ua: str) -> dict[str, str]:
    match = re.search(r"Chrome/(\d+)", ua or "")
    major = match.group(1) if match else str(CHROME_MAJOR)
    return {
        "sec-ch-ua": f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def normalize_proxy_url(raw: str) -> str:
    """Normalize common proxy formats to a URL curl_cffi accepts.

    Supported:
      http://user:pass@host:port
      socks5://user:pass@host:port
      user:pass@host:port
      host:port:user:pass
      host:port
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    if "://" in cleaned:
        return cleaned
    if "@" in cleaned:
        return f"http://{cleaned}"
    parts = cleaned.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{cleaned}"
    return f"http://{cleaned}"


def mask_proxy_url(proxy: str) -> str:
    if not proxy:
        return ""
    return re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", proxy)


def load_proxy_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#"):
            lines.append(normalize_proxy_url(cleaned))
    return lines


class ProxyPool:
    """Lease proxies to workers.

    exclusive=True (default): one unique proxy slot per concurrent thread (sticky IPs).
    exclusive=False: rotating/dynamic gateway — all workers may share the same proxy URL
    (exit IP changes per connection on the provider side).
    """

    def __init__(self, proxies: list[str], *, exclusive: bool = True) -> None:
        self._proxies = list(proxies)
        self._exclusive = exclusive and len(proxies) > 0
        self._available = list(range(len(proxies)))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._rr = 0

    def __len__(self) -> int:
        return len(self._proxies)

    def lease(self, preferred_index: int | None = None) -> tuple[str, int]:
        if not self._proxies:
            return "", -1
        if not self._exclusive:
            with self._lock:
                if preferred_index is not None and 0 <= preferred_index < len(self._proxies):
                    slot = preferred_index
                else:
                    slot = self._rr % len(self._proxies)
                    self._rr += 1
                return self._proxies[slot], slot
        with self._cond:
            while not self._available:
                self._cond.wait(timeout=120)
                if not self._available:
                    raise RuntimeError("Timed out waiting for a free proxy slot.")
            if preferred_index is not None and preferred_index in self._available:
                slot = preferred_index
                self._available.remove(preferred_index)
            else:
                slot = self._available.pop(0)
            return self._proxies[slot], slot

    def release(self, slot: int) -> None:
        if slot < 0 or not self._exclusive:
            return
        with self._cond:
            if slot not in self._available:
                self._available.append(slot)
                self._available.sort()
            self._cond.notify()


def assign_sticky_proxy(proxies: list[str], row_index: int) -> str:
    """1:1 sticky account→proxy. row_index is 0-based."""
    if not proxies:
        return ""
    return proxies[row_index % len(proxies)]


def facebook_login_url(next_url: str = DOLA_HOME) -> str:
    query = urlencode(
        {
            "account_sdk_source": "web",
            "aid": DOLA_AID,
            "next": next_url,
            "platform": "facebook",
            "use_local_host": "0",
            "action": "login",
        }
    )
    return f"https://www.dola.com/passport/web/web_login/?{query}"


def facebook_oauth_dialog_url(next_url: str = FACEBOOK_XD_ARBITER) -> str:
    query = urlencode(
        {
            "client_id": FACEBOOK_APP_ID,
            "app_id": FACEBOOK_APP_ID,
            "redirect_uri": next_url,
            "response_type": "token,signed_request,graph_domain",
            "display": "popup",
            "sdk": "joey",
            "scope": "public_profile,email",
            "origin": "1",
        }
    )
    return f"https://www.facebook.com/v21.0/dialog/oauth?{query}"


def oauth_values_from_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    merged = {
        **{key: values[-1] for key, values in fragment.items() if values},
        **{key: values[-1] for key, values in query.items() if values},
    }
    return {
        "code": merged.get("code") or "",
        "access_token": merged.get("access_token") or "",
        "error": merged.get("error") or merged.get("error_description") or "",
    }


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._current = {"action": data.get("action") or "", "method": (data.get("method") or "get").lower(), "inputs": {}}
            return
        if tag in {"input", "button"} and self._current is not None:
            name = data.get("name")
            if name:
                self._current["inputs"][name] = data.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def parse_html_forms(html: str) -> list[dict[str, Any]]:
    parser = _FormParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return parser.forms
    return parser.forms


def pick_continue_form(forms: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked: list[tuple[int, dict[str, Any]]] = []
    for form in forms:
        action = str(form.get("action") or "").lower()
        names = {str(name).lower() for name in (form.get("inputs") or {})}
        score = 0
        if any(key in names for key in ("__confirm__", "confirm", "submit[continue]", "continue")):
            score += 5
        if "oauth" in action or "confirm" in action or "grant" in action:
            score += 4
        if "fb_dtsg" in names:
            score += 2
        if "login" in action and "oauth" not in action:
            score -= 3
        if score > 0:
            ranked.append((score, form))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked else None


def page_looks_like_facebook_login(url: str, html: str) -> bool:
    lowered = f"{url}\n{html[:4000]}".lower()
    return any(marker in lowered for marker in ("login.php", "log in to facebook", "forgotten password", "create new account"))


def page_looks_like_checkpoint(url: str, html: str) -> bool:
    lowered_url = (url or "").lower()
    if any(token in lowered_url for token in ("/checkpoint", "two_step", "approvals", "security/login")):
        return True
    head = (html or "")[:20000].lower()
    markers = (
        "confirm your identity",
        "unusual activity",
        "your account has been locked",
        "two_step_verification",
        "two-step verification",
        "login approvals",
        "we need more information",
        "suspicious activity",
        "enter the login code",
        "authenticate your account",
    )
    return any(marker in head for marker in markers)


def _json_from_text(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^for \(;;\);", "", (text or "").strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def access_token_from_status_body(text: str) -> str:
    payload = _json_from_text(text)
    auth = payload.get("authResponse") or payload.get("payload") or payload
    if isinstance(auth, dict):
        token = auth.get("accessToken") or auth.get("access_token") or ""
        if token:
            return str(token)
    match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', text) or re.search(r'"access_token"\s*:\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def _is_tls_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "curl: (35)" in text or "tls connect error" in text or "ssl" in text


def _is_retryable_harvest_error(exc: BaseException) -> bool:
    if _is_tls_error(exc):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "fb_dtsg/lsd",
            "gdp page did not expose",
            "proxy rate-limit",
            "timed out",
            "curl: (28)",
            "curl: (56)",
            "curl: (7)",
        )
    )


def make_session(timeout: int, *, proxy: str = "", user_agent: str = "") -> curl_requests.Session:
    """Build a curl_cffi session. With a proxy, probe TLS until an impersonate works."""
    ua = user_agent.strip() or CHROME_UA
    proxies = {"http": proxy, "https": proxy} if proxy.strip() else None
    base_headers = {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept-language": "en-US,en;q=0.9",
        "user-agent": ua,
        "upgrade-insecure-requests": "1",
        **chrome_client_hint_headers(ua),
    }
    last_exc: Exception | None = None
    for impersonate in CURL_IMPERSONATE_CANDIDATES:
        try:
            session = curl_requests.Session(
                impersonate=impersonate,
                timeout=timeout,
                allow_redirects=False,
                proxies=proxies,
            )
            session.headers.update(base_headers)
            if proxies:
                # chrome146+ often fails CONNECT/TLS to dola.com via HTTP proxies.
                session.get(
                    "https://www.dola.com/",
                    headers={"accept": "text/html", "upgrade-insecure-requests": "1"},
                )
            setattr(session, "_impersonate", impersonate)
            return session
        except Exception as exc:
            last_exc = exc
            continue
    if proxies and last_exc and _is_tls_error(last_exc):
        raise RuntimeError(
            f"Proxy TLS failed for all Chrome impersonates ({mask_proxy_url(proxy)}): {last_exc}"
        ) from last_exc
    session = curl_requests.Session(timeout=timeout, allow_redirects=False, proxies=proxies)
    session.headers.update(base_headers)
    setattr(session, "_impersonate", "")
    return session


def apply_facebook_cookies(session: curl_requests.Session, cookies: dict[str, str]) -> None:
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".facebook.com", path="/", secure=True)


def mint_facebook_browser_cookies(session: curl_requests.Session) -> list[str]:
    session.get(
        "https://www.facebook.com/",
        headers={
            "referer": "https://www.google.com/",
            "upgrade-insecure-requests": "1",
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-user": "?1",
        },
    )
    if not session.cookies.get("wd"):
        session.cookies.set("wd", "1280x800", domain=".facebook.com", path="/", secure=True)
    return [name for name in ("sb", "datr", "fr", "wd") if session.cookies.get(name)]


def dola_cookies(session: curl_requests.Session) -> list[dict[str, Any]]:
    return cookies_from_jar(session.cookies, domain_contains="dola.com")


def extract_html_redirect(html: str, current_url: str) -> str:
    patterns = (
        r'window\.location(?:\.href)?\s*=\s*[\'"]([^\'"]+)',
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\';>\s]+)',
        r'href=["\'](https://www\.dola\.com/passport/web/web_login_success[^"\']*)',
    )
    for pattern in patterns:
        match = re.search(pattern, html or "", re.I)
        if match:
            target = match.group(1).replace("&amp;", "&").replace("&amp", "&")
            return urljoin(current_url, target)
    return ""


def _request(session: curl_requests.Session, method: str, url: str, data: dict[str, str] | None = None, referer: str = "") -> Any:
    parsed = urlparse(url)
    referer = referer or DOLA_HOME
    headers = {
        "referer": referer,
        "origin": f"{urlparse(referer).scheme}://{urlparse(referer).netloc}" if urlparse(referer).netloc else "https://www.dola.com",
        "upgrade-insecure-requests": "1",
    }
    if parsed.netloc.endswith("facebook.com"):
        headers["sec-fetch-site"] = "cross-site"
        headers["sec-fetch-mode"] = "navigate"
        headers["sec-fetch-dest"] = "document"
    if method.upper() == "GET":
        return session.get(url, headers=headers)
    headers["content-type"] = "application/x-www-form-urlencoded"
    return session.post(url, data=data or {}, headers=headers)


def follow(session: curl_requests.Session, url: str, *, method: str = "GET", data: dict[str, str] | None = None, max_hops: int = 16) -> Any:
    current_url = url
    current_method = method
    current_data = data
    referer = DOLA_HOME
    response = None
    for _ in range(max_hops):
        response = _request(session, current_method, current_url, current_data, referer=referer)
        response.final_url = current_url  # type: ignore[attr-defined]
        if page_looks_like_checkpoint(current_url, response.text or ""):
            raise RuntimeError("Facebook checkpoint blocked the login. Pass the check in a browser, then export cookies again.")
        status = int(getattr(response, "status_code", 0) or 0)
        location = response.headers.get("location") or response.headers.get("Location") or ""
        html = response.text or ""
        html_redirect = extract_html_redirect(html, current_url)
        if html_redirect and html_redirect != current_url and status == 200:
            referer = current_url
            current_url = html_redirect
            current_method = "GET"
            current_data = None
            continue
        values = oauth_values_from_url(current_url)
        location_values = oauth_values_from_url(location if location.startswith("http") else urljoin(current_url, location)) if location else {}
        if location_values.get("code") or location_values.get("access_token"):
            absolute = location if location.startswith("http") else urljoin(current_url, location)
            response.final_url = absolute  # type: ignore[attr-defined]
            return response
        if values.get("code") or values.get("access_token"):
            break
        if status in {301, 302, 303, 307, 308} and location:
            referer = current_url
            current_url = urljoin(current_url, location)
            current_method = "GET" if status in {301, 302, 303} else current_method
            current_data = None
            continue
        form = pick_continue_form(parse_html_forms(html))
        if form and "facebook.com" in urlparse(current_url).netloc:
            referer = current_url
            current_url = urljoin(current_url, str(form.get("action") or current_url))
            current_method = str(form.get("method") or "post").upper()
            current_data = dict(form.get("inputs") or {})
            if current_method == "GET":
                current_url = current_url + ("&" if "?" in current_url else "?") + urlencode(current_data)
                current_data = None
            continue
        return response
    if response is not None:
        response.final_url = current_url  # type: ignore[attr-defined]
    return response


def assert_facebook_session(session: curl_requests.Session) -> None:
    response = follow(session, "https://m.facebook.com/")
    url = str(getattr(response, "final_url", None) or getattr(response, "url", "") or "")
    if page_looks_like_facebook_login(url, response.text or ""):
        raise RuntimeError("Facebook rejected these cookies. They are expired, incomplete, or checkpointed.")


def bootstrap_dola(session: curl_requests.Session) -> None:
    follow(session, "https://www.dola.com/")
    follow(session, f"https://www.dola.com/passport/web/account/info/v2/?account_sdk_source=web&aid={DOLA_AID}")


# Captured when Dola shows "Confirm Your Age" (decision=3 → modal → Confirm click).
AGE_GATE_NEED_CONFIRM_DECISIONS = {2, 3}
AGE_GATE_PC_VERSION = "3.38.0"


def _dola_region(session: curl_requests.Session) -> str:
    return (
        session.cookies.get("flow_user_country")
        or session.cookies.get("store-country-code")
        or "PK"
    )


def _alice_common_query(session: curl_requests.Session, *, device_id: str = "", web_id: str = "") -> str:
    """Shared query string for /alice/* web calls (a_bogus omitted; works for age_gate)."""
    region = _dola_region(session)
    tea = web_id or str(uuid.uuid4().int)[:19]
    did = device_id or tea
    return urlencode(
        {
            "version_code": "20800",
            "language": "en",
            "device_platform": "web",
            "doubao_device_platform": "web",
            "aid": DOLA_AID,
            "real_aid": DOLA_AID,
            "pkg_type": "release_version",
            "device_id": did,
            "pc_version": AGE_GATE_PC_VERSION,
            "doubao_pc_version": AGE_GATE_PC_VERSION,
            "web_id": tea,
            "tea_uuid": tea,
            "region": region,
            "sys_region": region,
            "samantha_web": "1",
            "web_platform": "browser",
            "use-olympus-account": "1",
            "web_tab_id": str(uuid.uuid4()),
        }
    )


def _alice_json_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://www.dola.com",
        "referer": DOLA_HOME,
    }


def seed_web_anon_ids(session: curl_requests.Session, *, debug: bool = False) -> tuple[str, str]:
    """Best-effort device_id/web_id from get_web_anon_id (same as browser before age_gate)."""
    query = _alice_common_query(session)
    url = f"https://www.dola.com/alice/user/get_web_anon_id?{query}"
    try:
        response = session.post(url, data="{}", headers=_alice_json_headers())
        payload = _json_from_text(response.text or "")
        web_id = str(payload.get("web_id") or "")
        uid = str(payload.get("uid") or "")
        _debug_log(f"  get_web_anon_id -> web_id={web_id[:12]}... uid={uid[:12]}...", debug=debug)
        return uid or web_id, web_id or uid
    except Exception as exc:
        _debug_log(f"  get_web_anon_id skipped: {exc}", debug=debug)
        tea = str(uuid.uuid4().int)[:19]
        return tea, tea


def confirm_age_gate_if_needed(session: curl_requests.Session, *, debug: bool = False) -> dict[str, Any]:
    """Detect Dola age gate via /alice/age_gate/check and auto-Confirm via /report.

    Browser capture (Confirm click):
      POST /alice/age_gate/check  {"scene":1}
        -> decision=3 (modal)
      POST /alice/age_gate/report {"scene":1,"pass":true}
        -> {"pass":true,"is_minor":false}
    """
    device_id, web_id = seed_web_anon_ids(session, debug=debug)
    query = _alice_common_query(session, device_id=device_id, web_id=web_id)
    headers = _alice_json_headers()
    result: dict[str, Any] = {"checked": False, "confirmed": False, "decision": None, "error": ""}

    check_url = f"https://www.dola.com/alice/age_gate/check?{query}"
    try:
        check_resp = session.post(check_url, data=json.dumps({"scene": 1}), headers=headers)
        check_payload = _json_from_text(check_resp.text or "")
    except Exception as exc:
        result["error"] = f"age_gate/check failed: {exc}"
        return result

    result["checked"] = True
    data = check_payload.get("data") if isinstance(check_payload.get("data"), dict) else {}
    decision = data.get("decision")
    result["decision"] = decision
    result["check"] = {"code": check_payload.get("code"), "data": data}
    _debug_log(f"  age_gate/check -> code={check_payload.get('code')} decision={decision}", debug=debug)

    needs_confirm = decision in AGE_GATE_NEED_CONFIRM_DECISIONS
    if not needs_confirm:
        # Already clear / no modal — nothing to report.
        return result

    report_url = f"https://www.dola.com/alice/age_gate/report?{query}"
    try:
        report_resp = session.post(
            report_url,
            data=json.dumps({"scene": 1, "pass": True}),
            headers=headers,
        )
        report_payload = _json_from_text(report_resp.text or "")
    except Exception as exc:
        result["error"] = f"age_gate/report failed: {exc}"
        return result

    report_data = report_payload.get("data") if isinstance(report_payload.get("data"), dict) else {}
    result["confirmed"] = bool(report_data.get("pass")) or report_payload.get("code") == 0
    result["report"] = {"code": report_payload.get("code"), "data": report_data}
    _debug_log(
        f"  age_gate/report -> code={report_payload.get('code')} pass={report_data.get('pass')}",
        debug=debug,
    )
    return result


def _debug_log(msg: str, *, debug: bool) -> None:
    if debug:
        print(msg, flush=True)


# Green + bold success lines (Windows Terminal / modern consoles support ANSI).
_ANSI_GREEN_BOLD = "\033[1;32m"
_ANSI_RESET = "\033[0m"


def _print_cookie_success(msg: str, *, debug: bool) -> None:
    """Highlight when Dola cookies are obtained/written. Bold green in --debug."""
    if debug:
        print(f"{_ANSI_GREEN_BOLD}{msg}{_ANSI_RESET}", flush=True)
    else:
        print(msg, flush=True)


def jazoest_from_dtsg(fb_dtsg: str) -> str:
    return str(2 + sum(ord(char) for char in fb_dtsg))


def first_match(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def pick_experience_id(html: str) -> str:
    matches = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", html)
    if not matches:
        return str(uuid.uuid4())
    return max(set(matches), key=matches.count)


def scrape_fb_page_context(html: str, *, actor_id: str) -> dict[str, str]:
    ctx: dict[str, str] = {"actor_id": actor_id}
    ctx["fb_dtsg"] = first_match(
        (
            r'"token"\s*:\s*"([^"]+)"[^}]*"DTSG',
            r'\["DTSGInitData",\[\],\{"token":"([^"]+)"',
            r'name="fb_dtsg"\s+value="([^"]+)"',
            r'"fb_dtsg"\s*:\s*"([^"]+)"',
        ),
        html,
    )
    ctx["lsd"] = first_match(
        (
            r'\["LSD",\[\],\{"token":"([^"]+)"',
            r'name="lsd"\s+value="([^"]+)"',
            r'"LSD",\[\],\{"token":"([^"]+)"',
            r'"lsd"\s*:\s*"([^"]+)"',
        ),
        html,
    )
    ctx["hsi"] = first_match((r'"hsi"\s*:\s*"(\d+)"', r'"hsi":(\d+)'), html)
    ctx["rev"] = first_match((r'"__spin_r"\s*:\s*(\d+)', r'"client_revision"\s*:\s*(\d+)'), html)
    ctx["spin_t"] = first_match((r'"__spin_t"\s*:\s*(\d+)',), html) or str(int(time.time()))
    ctx["__dyn"] = first_match((r'"__dyn"\s*:\s*"([^"]+)"',), html)
    ctx["__csr"] = first_match((r'"__csr"\s*:\s*"([^"]+)"',), html)
    ctx["experience_id"] = first_match(
        (
            r'"experience_id"\s*:\s*"([0-9a-f-]{36})"',
            r'"id"\s*:\s*"([0-9a-f-]{36})"[^}]*"flows"\s*:\s*\[\{"id"\s*:\s*"gdp"',
            r'__aectx__[^"]*"id\\":\\"([0-9a-f-]{36})',
        ),
        html,
    )
    ctx["logger_id"] = first_match(
        (
            r'logger_id\\":\\"([0-9a-f]{16})\\"',
            r'params\[logger_id\]%22%3D%22([0-9a-f]{16})%22',
            r'logger_id=([0-9a-f]{16})',
            r'"logger_id"\s*:\s*"([0-9a-f]{16})"',
        ),
        html,
    )
    if ctx.get("fb_dtsg"):
        ctx["jazoest"] = jazoest_from_dtsg(ctx["fb_dtsg"])
    return ctx


def random_cb() -> str:
    return secrets.token_hex(8)


def dola_fb_origin_token() -> str:
    """Facebook JS SDK uses a fresh ~17-hex channel path under www.dola.com."""
    return secrets.token_hex(9)[:17]


def _xd_arbiter_origin_for_hash(token: str | None = None) -> str:
    """Origin value embedded in xd_arbiter hash (single-percent-encoded)."""
    tok = token or dola_fb_origin_token()
    return "https%3A%2F%2Fwww.dola.com%2F" + tok


def _xd_arbiter_origin_for_extra_params(token: str | None = None) -> str:
    """Origin fragment inside Sahara extra_params redirect_uri JSON string."""
    tok = token or dola_fb_origin_token()
    return "https\\u00253A\\u00252F\\u00252Fwww.dola.com\\u00252F" + tok


def quoted_xd_arbiter_for_extra_params(*, cb: str, frame: str, origin_token: str | None = None) -> str:
    inner = (
        f"https:\\/\\/staticxx.facebook.com\\/x\\/connect\\/xd_arbiter\\/?version=46"
        f"#cb={cb}&domain=www.dola.com&is_canvas=false&origin={_xd_arbiter_origin_for_extra_params(origin_token)}"
        f"&relation=opener&frame={frame}"
    )
    return f'"{inner}"'


def build_xd_arbiter_redirect(*, cb: str, frame: str | None = None, origin_token: str | None = None) -> str:
    origin = _xd_arbiter_origin_for_hash(origin_token)
    base = (
        f"https://staticxx.facebook.com/x/connect/xd_arbiter/?version=46"
        f"#cb={cb}&domain=www.dola.com&is_canvas=false&origin={origin}&relation=opener"
    )
    if frame:
        base += f"&frame={frame}"
    return base


def parse_redirect_parts(redirect_uri: str) -> tuple[str, str, str]:
    """Return (cb, frame, origin_token) from an xd_arbiter redirect_uri."""
    cb = first_match((r"#cb=([0-9a-f]+)", r"cb=([0-9a-f]+)"), redirect_uri) or random_cb()
    frame = first_match((r"frame=([0-9a-f]+)",), redirect_uri) or secrets.token_hex(8)
    token = first_match(
        (
            r"www\.dola\.com(?:%2F|%252F|/|\\u00252F)([a-f0-9]{15,20})",
            r"dola\.com/([a-f0-9]{15,20})",
        ),
        redirect_uri,
    )
    return cb, frame, token


def build_sdk_oauth_url(*, logger_id: str, cb: str, frame: str | None = None, origin_token: str | None = None) -> str:
    token = origin_token or dola_fb_origin_token()
    redirect_uri = build_xd_arbiter_redirect(cb=cb, frame=frame, origin_token=token)
    channel_url = build_xd_arbiter_redirect(cb=random_cb(), origin_token=token)
    query = urlencode(
        {
            "app_id": FACEBOOK_APP_ID,
            "cbt": str(int(time.time() * 1000)),
            "channel_url": channel_url,
            "client_id": FACEBOOK_APP_ID,
            "display": "popup",
            "domain": "www.dola.com",
            "e2e": "{}",
            "fallback_redirect_uri": DOLA_HOME,
            "locale": "en_US",
            "logger_id": logger_id,
            "origin": "1",
            "redirect_uri": redirect_uri,
            "response_type": "token,signed_request,graph_domain",
            "sdk": "joey",
            "version": "v18.0",
        }
    )
    return f"https://www.facebook.com/v18.0/dialog/oauth?{query}"


def parse_gdp_params(gdp_url: str) -> dict[str, str]:
    parsed = urlparse(gdp_url)
    query = parse_qs(parsed.query)
    nested: dict[str, str] = {}
    for key, values in query.items():
        if key.startswith("params[") and key.endswith("]") and values:
            nested[key[len("params[") : -1]] = values[-1].strip('"')
    return nested


def quote_redirect_uri_for_extra_params(raw_redirect: str) -> str:
    """Format GDP redirect_uri the way Sahara extra_params_json expects."""
    cleaned = (raw_redirect or "").strip().strip('"')
    if not cleaned:
        return '""'
    cleaned = cleaned.replace("\\/", "/")
    escaped: list[str] = []
    i = 0
    while i < len(cleaned):
        if cleaned.startswith("\\u0025", i):
            escaped.append("\\u0025")
            i += 6
            continue
        ch = cleaned[i]
        if ch == "%" and i + 2 < len(cleaned):
            escaped.append("\\u0025")
            escaped.append(cleaned[i + 1 : i + 3])
            i += 3
            continue
        if ch == "/":
            escaped.append("\\/")
            i += 1
            continue
        escaped.append(ch)
        i += 1
    return f'"{"".join(escaped)}"'


def build_extra_params_json(gdp_params: dict[str, str], *, experience_id: str) -> str:
    """Build Sahara extra_params_json from the LIVE GDP URL params (keep real origin).

    Note: GDP page URL may say next=confirm, but Sahara GraphQL (browser) still sends
    next=read + steps baseline/public_profile. Mixing confirm into GraphQL → field_exception.
    """
    raw_redirect = (gdp_params.get("redirect_uri") or "").strip().strip('"')
    raw_redirect = (
        raw_redirect.replace("\\/", "/")
        .replace("\\u00253A", "%3A")
        .replace("\\u00252F", "%2F")
    )
    logger_id = (gdp_params.get("logger_id") or secrets.token_hex(8)).strip('"')
    aectx = json.dumps(
        {"id": experience_id, "flows": [{"id": "gdp", "prompts": [{"id": "gdp_read"}]}]},
        separators=(",", ":"),
    )
    if raw_redirect and "xd_arbiter" in raw_redirect:
        redirect_quoted = quote_redirect_uri_for_extra_params(raw_redirect)
    else:
        cb, frame, token = parse_redirect_parts(raw_redirect)
        redirect_quoted = quoted_xd_arbiter_for_extra_params(cb=cb, frame=frame, origin_token=token or None)
    payload = {
        "app_id": gdp_params.get("app_id") or FACEBOOK_APP_ID,
        "display": '"popup"',
        "domain": '"www.dola.com"',
        "fallback_redirect_uri": '"https:\\/\\/www.dola.com\\/chat\\/"',
        "logger_id": f'"{logger_id}"',
        "next": '"read"',
        "redirect_uri": redirect_quoted,
        "response_type": '"token,signed_request,graph_domain"',
        "scope": "[]",
        "sdk": '"joey"',
        "steps": '{"read":["baseline","public_profile"]}',
        "versioned_sdk": '"joey"',
        "cui_gk": '"[PASS]:jssdk,read"',
        "__aectx__": aectx,
    }
    return json.dumps(payload, separators=(",", ":"))


def sahara_input(
    *,
    actor_id: str,
    experience_id: str,
    extra_params_json: str,
    client_mutation_id: str,
    event: str | None = None,
    event_data_json: str | None = None,
) -> dict[str, Any]:
    # Keep device_id:null — browser always sends it; omitting still fails some accounts.
    data: dict[str, Any] = {
        "actor_id": actor_id,
        "client_mutation_id": client_mutation_id,
        "device_id": None,
        "experience_id": experience_id,
        "extra_params_json": extra_params_json,
    }
    if event:
        data["event"] = event
        data["event_data_json"] = event_data_json or "{}"
    else:
        data.update(
            {
                "flow": "GDP",
                "inputs_json": '{"public_profile":"true","baseline":"true","gdp_error_custom_component_input":""}',
                "outcome": "APPROVED",
                "outcome_data_json": "{}",
                "prompt": "GDP_READ",
                "runtime": "SAHARA",
                "source": "gdp_delegated",
                "surface": "FACEBOOK_COMET",
            }
        )
    return {"input": data}


def graphql_post(
    session: curl_requests.Session,
    *,
    friendly_name: str,
    doc_id: str,
    variables: dict[str, Any],
    ctx: dict[str, str],
    referer: str,
    req_num: int,
) -> dict[str, Any]:
    actor_id = ctx["actor_id"]
    form = {
        "av": actor_id,
        "__user": actor_id,
        "__a": "1",
        "__req": str(req_num),
        "__hs": ctx.get("__hs") or "20721.HYP:comet_plat_default_pkg.2.1...0",
        "dpr": "1",
        "__ccg": "EXCELLENT",
        "__rev": ctx.get("rev") or "1047958339",
        "__s": secrets.token_hex(3) + ":" + secrets.token_hex(3) + ":" + secrets.token_hex(3),
        "__hsi": ctx.get("hsi") or str(random.randint(10**18, 10**19)),
        "__comet_req": "1",
        "fb_dtsg": ctx["fb_dtsg"],
        "jazoest": ctx.get("jazoest") or jazoest_from_dtsg(ctx["fb_dtsg"]),
        "lsd": ctx["lsd"],
        "__spin_r": ctx.get("rev") or "1047958339",
        "__spin_b": "trunk",
        "__spin_t": ctx.get("spin_t") or str(int(time.time())),
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": friendly_name,
        "variables": json.dumps(variables, separators=(",", ":")),
        "doc_id": doc_id,
        "server_timestamps": "true",
    }
    for key in ("__dyn", "__csr"):
        if ctx.get(key):
            form[key] = ctx[key]
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.facebook.com",
        "referer": referer,
        "x-fb-friendly-name": friendly_name,
        "x-fb-lsd": ctx["lsd"],
        "accept": "*/*",
    }
    response = session.post(GRAPHQL_URL, data=form, headers=headers)
    return {"status": response.status_code, "payload": _json_from_text(response.text or "")}


def extract_consent_complete_url(payload: dict[str, Any]) -> str:
    outcome = payload.get("data", {}).get("post_prompt_outcome") or {}
    action = outcome.get("finish_flow_action") or {}
    uri = str(((action.get("link_uri") or {}).get("uri")) or "")
    if "/dialog/consent/complete/" in uri:
        return uri
    blob = json.dumps(payload)
    match = re.search(r"https://www\.facebook\.com/dialog/consent/complete/\?[^\"\\]+", blob)
    if match:
        return match.group(0).replace("\\/", "/")
    if "error_code" in uri or "gdp_error" in uri:
        error_code = ""
        match = re.search(r"error_code[=%](\d+)", uri) or re.search(r"params\[error_code\]=(\d+)", uri)
        if match:
            error_code = match.group(1)
        detail = f" error_code={error_code}" if error_code else ""
        raise RuntimeError(
            f"Sahara GDP rejected consent{detail}. "
            "Usually Facebook checkpoint/2FA or dead cookies — not a bulk-import bug."
        )
    flow_outcome = str(outcome.get("flow_outcome") or "")
    if flow_outcome and flow_outcome != "APPROVED":
        raise RuntimeError(f"Sahara outcome not approved: {flow_outcome}")
    return ""


def follow_for_access_token(session: curl_requests.Session, start_url: str, *, referer: str, debug: bool) -> str:
    current = start_url
    for hop in range(12):
        _debug_log(f"  hop {hop}: {current[:120]}...", debug=debug)
        response = session.get(
            current,
            headers={"referer": referer, "upgrade-insecure-requests": "1"},
            allow_redirects=False,
        )
        joined = str(response.url or current)
        values = oauth_values_from_url(joined)
        if values.get("access_token"):
            return values["access_token"]
        location = response.headers.get("location") or response.headers.get("Location") or ""
        if location:
            absolute = urljoin(current, location)
            values = oauth_values_from_url(absolute)
            if values.get("access_token"):
                return values["access_token"]
            current = absolute
            referer = joined
            continue
        html = response.text or ""
        match = re.search(r'#access_token=([^&"\']+)', html)
        if match:
            return match.group(1)
        match = re.search(r'access_token=([^&"\']+)', html)
        if match and match.group(1).startswith("EAA"):
            return match.group(1)
        break
    return ""


def facebook_openid(session: curl_requests.Session, access_token: str) -> str:
    response = session.get(
        "https://graph.facebook.com/me?fields=id",
        params={"access_token": access_token},
        headers={"accept": "application/json"},
    )
    return str(_json_from_text(response.text or "").get("id") or "")


def dola_passport_login(
    session: curl_requests.Session,
    *,
    access_token: str,
    openid: str,
    profile_key: str = "",
    debug: bool,
) -> list[dict[str, Any]]:
    base_query = urlencode(
        {
            "device_platform": "web",
            "os_type": "1",
            "terminal_type": "2",
            "aid": DOLA_AID,
            "account_sdk_source": "web",
            "passport_jssdk_version": "2.0.1-verify-center.1",
            "language": "en",
        }
    )
    headers = {
        "origin": "https://www.dola.com",
        "referer": DOLA_HOME,
        "accept": "application/json, text/plain, */*",
        "content-type": "application/x-www-form-urlencoded",
    }
    csrf = session.cookies.get("passport_csrf_token") or session.cookies.get("passport_csrf_token_default")
    if csrf:
        headers["x-tt-passport-csrf-token"] = csrf
    body: dict[str, str] = {
        "platform_app_id": DOLA_FB_PLATFORM_APP_ID,
        "access_token": access_token,
        "openid": openid,
    }
    if profile_key:
        body["profile_key"] = profile_key
    for path in ("/passport/web/auth/login_only/", "/passport/web/auth/login/"):
        url = f"https://www.dola.com{path}?{base_query}"
        response = session.post(url, data=body, headers=headers)
        cookies = dola_cookies(session)
        if has_dola_auth(cookies):
            return cookies
        payload = _json_from_text(response.text or "")
        _debug_log(
            f"  {path} -> {payload.get('message')} {((payload.get('data') or {}).get('description') or '')[:80]}",
            debug=debug,
        )
        data = payload.get("data") or {}
        if not profile_key and data.get("profile_key"):
            profile_key = str(data["profile_key"])
            body["profile_key"] = profile_key
    cookies = dola_cookies(session)
    if has_dola_auth(cookies):
        return cookies
    raise RuntimeError("Dola passport login did not set session cookies.")


def _gdp_html_has_tokens(html: str) -> bool:
    low = (html or "").lower()
    if "sorry, something went wrong" in low:
        return False
    if len(html or "") < 5000:
        return False
    return bool(
        "fb_dtsg" in (html or "")
        or "DTSGInitialData" in (html or "")
        or '"DTSG"' in (html or "")
        or "DTSGInitData" in (html or "")
    )


def _fetch_fallback_gdp(
    session: curl_requests.Session,
    *,
    logger_id: str,
    cb: str,
    frame: str,
    origin_token: str,
    debug: bool,
) -> tuple[str, str]:
    redirect = build_xd_arbiter_redirect(cb=cb, frame=frame, origin_token=origin_token)
    gdp_query = urlencode(
        {
            "flow": "gdp",
            "params[app_id]": FACEBOOK_APP_ID,
            "params[display]": '"popup"',
            "params[domain]": '"www.dola.com"',
            "params[fallback_redirect_uri]": '"https:\\/\\/www.dola.com\\/chat\\/"',
            "params[logger_id]": f'"{logger_id}"',
            "params[next]": '"confirm"',
            "params[redirect_uri]": f'"{redirect}"',
            "params[response_type]": '"token,signed_request,graph_domain"',
            "params[scope]": "[]",
            "params[sdk]": '"joey"',
            "params[steps]": "{}",
            "params[versioned_sdk]": '"joey"',
            "params[cui_gk]": '"[PASS]:jssdk,confirm"',
            "source": "gdp_delegated",
            "cache_buster": str(random.randint(-10**18, 10**18)),
        }
    )
    gdp_url = f"https://www.facebook.com/privacy/consent/?{gdp_query}"
    _debug_log(f"fallback GDP -> {gdp_url[:100]}...", debug=debug)
    response = session.get(
        gdp_url,
        headers={
            "referer": DOLA_HOME,
            "upgrade-insecure-requests": "1",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-user": "?1",
        },
    )
    return str(response.url or gdp_url), response.text or ""


def fetch_gdp_page(session: curl_requests.Session, *, debug: bool) -> tuple[str, str, dict[str, str]]:
    logger_id = secrets.token_hex(8)
    cb = random_cb()
    frame = secrets.token_hex(8)
    origin_token = dola_fb_origin_token()
    oauth_url = build_sdk_oauth_url(logger_id=logger_id, cb=cb, frame=frame, origin_token=origin_token)
    _debug_log(f"SDK oauth -> {oauth_url[:100]}...", debug=debug)
    response = follow(session, oauth_url)
    final_url = str(getattr(response, "final_url", "") or getattr(response, "url", "") or "")
    html = response.text or ""
    if page_looks_like_checkpoint(final_url, html):
        raise RuntimeError(
            "Facebook checkpoint/2FA blocked OAuth before GDP consent. "
            "Unlock the account in a browser, then re-export cookies."
        )

    land_ok = "/privacy/consent/" in final_url and _gdp_html_has_tokens(html)
    if not land_ok:
        # Concurrent/proxy bursts often return an empty/error GDP shell — retry with fresh params.
        last_html = html
        last_url = final_url
        for attempt in range(1, 4):
            logger_id = secrets.token_hex(8)
            cb = random_cb()
            frame = secrets.token_hex(8)
            origin_token = dola_fb_origin_token()
            last_url, last_html = _fetch_fallback_gdp(
                session,
                logger_id=logger_id,
                cb=cb,
                frame=frame,
                origin_token=origin_token,
                debug=debug,
            )
            if page_looks_like_checkpoint(last_url, last_html):
                raise RuntimeError(
                    "Facebook checkpoint/2FA on GDP page. Unlock in browser, then re-export cookies."
                )
            if "/privacy/consent/" in last_url and _gdp_html_has_tokens(last_html):
                final_url, html = last_url, last_html
                break
            _debug_log(
                f"  GDP tokens missing (attempt {attempt}/3, html={len(last_html)}), retry...",
                debug=debug,
            )
            time.sleep(0.6 * attempt + random.random() * 0.4)
        else:
            final_url, html = last_url, last_html

    if "/privacy/consent/" not in final_url:
        raise RuntimeError(f"Expected GDP consent page, got {urlparse(final_url).path or final_url[:120]}")
    actor_id = session.cookies.get("c_user") or ""
    ctx = scrape_fb_page_context(html, actor_id=actor_id)
    if not ctx.get("fb_dtsg") or not ctx.get("lsd"):
        raise RuntimeError(
            "GDP page did not expose fb_dtsg/lsd "
            f"(html={len(html)}, likely proxy rate-limit — retry with fewer workers)."
        )
    gdp_params = parse_gdp_params(final_url)
    if not ctx.get("logger_id"):
        ctx["logger_id"] = gdp_params.get("logger_id") or logger_id
    if not ctx.get("experience_id"):
        ctx["experience_id"] = pick_experience_id(html)
    return final_url, html, ctx


def replay_sahara_consent(session: curl_requests.Session, gdp_url: str, ctx: dict[str, str], *, debug: bool) -> str:
    extra_params_json = build_extra_params_json(parse_gdp_params(gdp_url), experience_id=ctx["experience_id"])
    req = 1

    def mutate(kind: str, variables: dict[str, Any]) -> dict[str, Any]:
        nonlocal req
        doc_id, friendly = SAHARA_DOC_IDS[kind]
        req += 1
        result = graphql_post(
            session,
            friendly_name=friendly,
            doc_id=doc_id,
            variables=variables,
            ctx=ctx,
            referer=gdp_url,
            req_num=req,
        )
        _debug_log(f"  GraphQL {kind} -> HTTP {result['status']}", debug=debug)
        if result["status"] != 200:
            raise RuntimeError(f"GraphQL {kind} failed: HTTP {result['status']}")
        errors = result["payload"].get("errors") or []
        if errors and kind in {"validation", "outcome"}:
            raise RuntimeError(f"GraphQL {kind} error: {errors[0].get('message')}")
        if errors:
            _debug_log(f"  GraphQL {kind} warning: {errors[0].get('message')}", debug=debug)
        return result["payload"]

    base = {
        "actor_id": ctx["actor_id"],
        "experience_id": ctx["experience_id"],
        "extra_params_json": extra_params_json,
    }
    mutate("interactions", sahara_input(**base, client_mutation_id="1", event="PROMPT_IMPRESSION", event_data_json='{"prompt_type":8}'))
    mutate("interactions", sahara_input(**base, client_mutation_id="2", event="CONTENT_IMPRESSION", event_data_json="{}"))
    validation_payload = mutate("validation", sahara_input(**base, client_mutation_id="3"))
    validation = (validation_payload.get("data") or {}).get("consent_prompt_validation") or {}
    typename = str(validation.get("__typename") or "")
    if typename and "AllClear" not in typename:
        err_text = ""
        errors = validation.get("errors") or []
        if errors and isinstance(errors[0], dict):
            err_text = str(((errors[0].get("error_message") or {}).get("text")) or errors[0])
        raise RuntimeError(f"Sahara validation failed ({typename}): {err_text or 'unknown'}")
    outcome_payload = mutate("outcome", sahara_input(**base, client_mutation_id="4"))
    complete_url = extract_consent_complete_url(outcome_payload)
    if not complete_url:
        raise RuntimeError("PostPromptOutcome did not return consent/complete URL.")
    _debug_log(f"  consent/complete URL captured ({len(complete_url)} chars)", debug=debug)
    return complete_url


def harvest_one(
    facebook_cookies: dict[str, str],
    *,
    timeout_seconds: int = 90,
    debug: bool = False,
    ctx: HarvestContext | None = None,
) -> dict[str, Any]:
    harvest_ctx = ctx or HarvestContext(debug=debug)
    prefix = harvest_ctx.log_prefix or ""
    ua = harvest_ctx.user_agent or desktop_ua_for_c_user(facebook_c_user(facebook_cookies) or "unknown")
    fb_session = make_session(timeout_seconds, proxy=harvest_ctx.proxy, user_agent=ua)
    apply_facebook_cookies(fb_session, facebook_cookies)
    minted = mint_facebook_browser_cookies(fb_session)
    if minted:
        print(f"{prefix}  minted Facebook cookies: {', '.join(minted)}")
    if harvest_ctx.proxy:
        print(f"{prefix}  proxy={mask_proxy_url(harvest_ctx.proxy)}")
    print(f"{prefix}  UA={ua}")
    imp = getattr(fb_session, "_impersonate", "")
    if imp:
        print(f"{prefix}  tls={imp}")
    assert_facebook_session(fb_session)

    gdp_url, _html, page_ctx = fetch_gdp_page(fb_session, debug=harvest_ctx.debug)
    complete_url = replay_sahara_consent(fb_session, gdp_url, page_ctx, debug=harvest_ctx.debug)
    access_token = follow_for_access_token(fb_session, complete_url, referer=gdp_url, debug=harvest_ctx.debug)
    if not access_token:
        raise RuntimeError("consent/complete did not yield access_token.")

    openid = facebook_openid(fb_session, access_token)
    if not openid:
        raise RuntimeError("Could not resolve Facebook openid from access_token.")

    dola_session = make_session(timeout_seconds, proxy=harvest_ctx.proxy, user_agent=ua)
    bootstrap_dola(dola_session)
    cookies = filter_dola_cookies(
        dola_passport_login(dola_session, access_token=access_token, openid=openid, debug=harvest_ctx.debug)
    )
    if not has_dola_auth(cookies):
        raise RuntimeError("Dola auth cookies missing after passport login.")

    age = confirm_age_gate_if_needed(dola_session, debug=harvest_ctx.debug)
    if age.get("confirmed"):
        print(f"{prefix}  age gate: auto-confirmed (decision={age.get('decision')})")
    elif age.get("checked") and age.get("decision") in AGE_GATE_NEED_CONFIRM_DECISIONS:
        print(f"{prefix}  age gate: confirm attempted but not passed ({age.get('error') or age.get('report')})")
    elif age.get("checked"):
        print(f"{prefix}  age gate: no confirm needed (decision={age.get('decision')})")
    elif age.get("error"):
        print(f"{prefix}  age gate: {age['error']}")

    # Re-read cookies in case report set anything new (usually unchanged).
    cookies = filter_dola_cookies(dola_cookies(dola_session)) or cookies
    return {
        "cookies": cookies,
        "strategy": "harvest_via_http",
        "user_agent": ua,
        "age_gate": age,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest Dola cookies from Facebook cookies (HTTP only).")
    parser.add_argument("--file", type=Path, default=DEFAULT_INPUT, help="Facebook cookie header file.")
    parser.add_argument("--cookies", default="", help="Single Facebook cookie header string.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output folder.")
    parser.add_argument("--timeout", type=int, default=90, help="HTTP timeout in seconds.")
    parser.add_argument("--debug", action="store_true", help="Print HTTP replay steps.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel harvest workers (default 1). Capped to proxy count when --proxy-file is set.")
    parser.add_argument(
        "--proxy-file",
        type=Path,
        default=None,
        help="One proxy per line. Each concurrent thread leases its own proxy. Sticky: line i prefers proxy i.",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Ignore input/proxies.txt and run direct (no proxy).",
    )
    parser.add_argument(
        "--rotating-proxy",
        action="store_true",
        help="Proxy gateway rotates exit IPs. Workers share proxy URL(s); do not exclusive-lease.",
    )
    parser.add_argument(
        "--no-cap-workers-to-proxies",
        action="store_true",
        help="Allow more workers than proxies (proxies will be reused after lease release).",
    )
    parser.add_argument("--daily-limit", type=int, default=2, help="daily_limit written into bulk import JSON.")
    parser.add_argument("--bulk-out", type=Path, default=None, help="Bulk AUTO-DOLA import JSON path.")
    return parser.parse_args(argv)


def load_rows(args: argparse.Namespace) -> list[dict[str, str]]:
    chunks: list[str] = []
    if args.cookies.strip():
        chunks.append(args.cookies)
    if args.file.exists():
        chunks.append(args.file.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    for chunk in chunks:
        rows.extend(load_facebook_cookie_rows(chunk))
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        unique[facebook_c_user(row) or cookie_header(row)] = row
    return list(unique.values())


def _ledger_error(output: Path, stamp: str, c_user: str, status: str, error: str, *, lock: threading.Lock | None = None) -> None:
    row = {
        "timestamp": stamp,
        "facebook_c_user": c_user,
        "status": status,
        "dola_auth": "no",
        "cookie_names": "",
        "cookie_header": "",
        "json_path": "",
        "error": error,
    }
    if lock:
        with lock:
            append_ledger_row(output, row)
    else:
        append_ledger_row(output, row)


def _process_row(
    *,
    index: int,
    total: int,
    facebook_cookies: dict[str, str],
    preferred_proxy_index: int,
    proxy_pool: ProxyPool | None,
    args: argparse.Namespace,
    ledger_lock: threading.Lock,
) -> dict[str, Any]:
    c_user = facebook_c_user(facebook_cookies) or "?"
    stamp = utc_stamp()
    prefix = f"[{index}/{total} {c_user}]"
    recommended = [name for name in RECOMMENDED_FACEBOOK_COOKIES if name not in facebook_cookies and name != "sb"]
    missing = missing_facebook_cookies(facebook_cookies)
    print(f"{prefix} start")
    if recommended:
        print(f"{prefix} missing recommended cookies: {', '.join(recommended)}")
    if missing:
        error = f"missing {', '.join(missing)}"
        print(f"{prefix} skipped, {error}", file=sys.stderr)
        _ledger_error(args.output, stamp, c_user, "skipped", error, lock=ledger_lock)
        return {"ok": False, "facebook_c_user": c_user, "error": error}

    proxy = ""
    proxy_slot = -1
    if proxy_pool and len(proxy_pool) > 0:
        proxy, proxy_slot = proxy_pool.lease(preferred_proxy_index)
        print(f"{prefix} leased proxy slot={proxy_slot} {mask_proxy_url(proxy)}")

    ctx = HarvestContext(
        proxy=proxy,
        user_agent=desktop_ua_for_c_user(c_user),
        debug=args.debug,
        log_prefix=f"{prefix} ",
    )
    try:
        last_exc: Exception | None = None
        result: dict[str, Any] | None = None
        for attempt in range(1, 4):
            try:
                result = harvest_one(facebook_cookies, timeout_seconds=args.timeout, ctx=ctx)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 3 and _is_retryable_harvest_error(exc):
                    print(f"{prefix} retryable error (attempt {attempt}/3): {exc}", flush=True)
                    time.sleep(0.8 * attempt)
                    continue
                raise
        assert result is not None
        cookies = result["cookies"]
        paths = write_profile_files(args.output, facebook_c_user_id=c_user, cookies=cookies, stamp=stamp)
        profiles_dir = args.output / "profiles"
        uid_path = write_uid_profile(profiles_dir, c_user, cookies)
        with ledger_lock:
            append_ledger_row(
                args.output,
                {
                    "timestamp": stamp,
                    "facebook_c_user": c_user,
                    "status": "ok",
                    "dola_auth": "yes",
                    "cookie_names": ",".join(cookie_names(cookies)),
                    "cookie_header": cookie_header(cookies),
                    "json_path": str(paths["json"]),
                    "error": "",
                },
            )
        _print_cookie_success(
            f"{prefix} saved {paths['json'].name} + {uid_path.name} via {result['strategy']} ({len(cookies)} cookies)",
            debug=bool(args.debug),
        )
        return {
            "ok": True,
            "facebook_c_user": c_user,
            "profile": profile_entry(name=c_user, cookies=cookies, daily_limit=args.daily_limit),
            "proxy": mask_proxy_url(proxy),
        }
    except Exception as exc:
        print(f"{prefix} failed: {exc}", file=sys.stderr)
        _ledger_error(args.output, stamp, c_user, "error", str(exc), lock=ledger_lock)
        return {"ok": False, "facebook_c_user": c_user, "error": str(exc), "proxy": mask_proxy_url(proxy)}
    finally:
        if proxy_pool is not None:
            proxy_pool.release(proxy_slot)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = load_rows(args)
    if not rows:
        print(f"No Facebook cookies found. Copy fb_cookies.example.txt to {args.file} and paste your cookie header.", file=sys.stderr)
        return 2

    proxies: list[str] = []
    if args.no_proxy:
        proxies = []
    else:
        proxy_path = args.proxy_file or (ROOT / "input" / "proxies.txt")
        proxies = load_proxy_lines(proxy_path) if proxy_path.exists() else []
        if args.proxy_file and not proxies:
            print(f"Warning: proxy file empty or missing: {args.proxy_file}", file=sys.stderr)

    workers = max(1, int(args.workers))
    rotating = bool(getattr(args, "rotating_proxy", False))
    # Single gateway + many workers ⇒ almost always a rotating/dynamic residential endpoint.
    if proxies and workers > len(proxies) and (rotating or len(proxies) == 1):
        rotating = True
        print(
            f"Rotating/dynamic proxy mode: {len(proxies)} gateway(s) shared across {workers} workers "
            f"(exit IP changes per connection)."
        )
    elif proxies and not args.no_cap_workers_to_proxies and workers > len(proxies):
        print(
            f"Capping workers {workers} -> {len(proxies)} (sticky 1:1 proxy). "
            f"For dynamic IP gateway use --rotating-proxy or --no-cap-workers-to-proxies."
        )
        workers = len(proxies)

    proxy_pool = ProxyPool(proxies, exclusive=not rotating) if proxies else None
    if proxies:
        mode = "rotating/shared" if rotating else "sticky/exclusive"
        print(
            f"Harvesting {len(rows)} account(s) with {workers} worker(s), "
            f"{len(proxies)} proxy(ies) [{mode}]"
        )
    bulk_out = args.bulk_out or (args.output / "auto_dola_bulk_import.json")
    ledger_lock = threading.Lock()
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    tasks: list[tuple[int, dict[str, str], int]] = []
    for index, facebook_cookies in enumerate(rows, start=1):
        preferred = (index - 1) % len(proxies) if proxies else -1
        tasks.append((index, facebook_cookies, preferred))

    if not proxies:
        print(f"Harvesting {len(tasks)} account(s) with {workers} worker(s) (no proxies)")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _process_row,
                index=index,
                total=len(tasks),
                facebook_cookies=facebook_cookies,
                preferred_proxy_index=preferred,
                proxy_pool=proxy_pool,
                args=args,
                ledger_lock=ledger_lock,
            )
            for index, facebook_cookies, preferred in tasks
        ]
        for future in as_completed(futures):
            result = future.result()
            if result.get("ok"):
                successes.append(result["profile"])
            else:
                failures.append(
                    {
                        "facebook_c_user": str(result.get("facebook_c_user") or ""),
                        "error": str(result.get("error") or "unknown"),
                    }
                )

    payload = build_bulk_payload(profiles=successes, failures=failures, default_daily_limit=args.daily_limit)
    write_bulk_import(bulk_out, payload)
    print(f"Bulk import wrote {bulk_out} (ok={len(successes)} failed={len(failures)})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

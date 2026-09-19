"""Harvest Dola cookies from Facebook cookies over pure HTTP (curl_cffi).

Put one Facebook cookie header per line in input/fb_cookies.txt, then run:

    python harvest_dola_cookies.py
    python harvest_dola_cookies.py --debug
"""

from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import sys
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import requests as curl_requests

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
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
    if "/checkpoint" in (url or "").lower():
        return True
    head = (html or "")[:2500].lower()
    return "confirm your identity" in head or "unusual activity" in head or "your account has been locked" in head


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


def make_session(timeout: int) -> curl_requests.Session:
    for impersonate in ("chrome131", "chrome124", "chrome120", "chrome110"):
        try:
            session = curl_requests.Session(impersonate=impersonate, timeout=timeout, allow_redirects=False)
            session.headers.update(
                {
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": "en-US,en;q=0.9",
                    "user-agent": CHROME_UA,
                }
            )
            return session
        except Exception:
            continue
    return curl_requests.Session(timeout=timeout, allow_redirects=False)


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
            return urljoin(current_url, match.group(1).replace("&amp;", "&"))
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


def _debug_log(msg: str, *, debug: bool) -> None:
    if debug:
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


def _xd_arbiter_origin_token() -> str:
    return "https\\u00253A\\u00252F\\u00252Fwww.dola.com\\u00252Ffe69603bd3ed892b8"


def quoted_xd_arbiter_for_extra_params(*, cb: str, frame: str) -> str:
    inner = (
        f"https:\\/\\/staticxx.facebook.com\\/x\\/connect\\/xd_arbiter\\/?version=46"
        f"#cb={cb}&domain=www.dola.com&is_canvas=false&origin={_xd_arbiter_origin_token()}"
        f"&relation=opener&frame={frame}"
    )
    return f'"{inner}"'


def build_xd_arbiter_redirect(*, cb: str, frame: str | None = None) -> str:
    origin = "https%253A%252F%252Fwww.dola.com%252Ffe69603bd3ed892b8"
    base = (
        f"https://staticxx.facebook.com/x/connect/xd_arbiter/?version=46"
        f"#cb={cb}&domain=www.dola.com&is_canvas=false&origin={origin}&relation=opener"
    )
    if frame:
        base += f"&frame={frame}"
    return base


def parse_redirect_parts(redirect_uri: str) -> tuple[str, str]:
    cb = first_match((r"#cb=([0-9a-f]+)", r"cb=([0-9a-f]+)"), redirect_uri) or random_cb()
    frame = first_match((r"frame=([0-9a-f]+)",), redirect_uri) or secrets.token_hex(8)
    return cb, frame


def build_sdk_oauth_url(*, logger_id: str, cb: str, frame: str | None = None) -> str:
    redirect_uri = build_xd_arbiter_redirect(cb=cb, frame=frame)
    channel_url = build_xd_arbiter_redirect(cb=random_cb())
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


def build_extra_params_json(gdp_params: dict[str, str], *, experience_id: str) -> str:
    raw_redirect = (gdp_params.get("redirect_uri") or "").strip('"')
    cb, frame = parse_redirect_parts(raw_redirect)
    logger_id = (gdp_params.get("logger_id") or secrets.token_hex(8)).strip('"')
    aectx = json.dumps(
        {"id": experience_id, "flows": [{"id": "gdp", "prompts": [{"id": "gdp_read"}]}]},
        separators=(",", ":"),
    )
    payload = {
        "app_id": gdp_params.get("app_id") or FACEBOOK_APP_ID,
        "display": '"popup"',
        "domain": '"www.dola.com"',
        "fallback_redirect_uri": '"https:\\/\\/www.dola.com\\/chat\\/"',
        "logger_id": f'"{logger_id}"',
        "next": '"read"',
        "redirect_uri": quoted_xd_arbiter_for_extra_params(cb=cb, frame=frame),
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
        "__hs": "20715.HYP:comet_plat_default_pkg.2.1...0",
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
        raise RuntimeError("Sahara returned GDP error redirect (extra_params_json likely invalid).")
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


def fetch_gdp_page(session: curl_requests.Session, *, debug: bool) -> tuple[str, str, dict[str, str]]:
    logger_id = secrets.token_hex(8)
    cb = random_cb()
    frame = secrets.token_hex(8)
    oauth_url = build_sdk_oauth_url(logger_id=logger_id, cb=cb, frame=frame)
    _debug_log(f"SDK oauth -> {oauth_url[:100]}...", debug=debug)
    response = follow(session, oauth_url)
    final_url = str(getattr(response, "final_url", "") or getattr(response, "url", "") or "")
    html = response.text or ""
    if "/privacy/consent/" not in final_url:
        gdp_query = urlencode(
            {
                "flow": "gdp",
                "params[app_id]": f'"{FACEBOOK_APP_ID}"',
                "params[display]": '"popup"',
                "params[domain]": '"www.dola.com"',
                "params[fallback_redirect_uri]": '"https://www.dola.com/chat/"',
                "params[logger_id]": f'"{logger_id}"',
                "params[next]": '"read"',
                "params[redirect_uri]": f'"{build_xd_arbiter_redirect(cb=cb, frame=frame)}"',
                "params[response_type]": '"token,signed_request,graph_domain"',
                "params[scope]": "[]",
                "params[sdk]": '"joey"',
                "params[steps]": '{"read":["baseline","public_profile"]}',
                "params[versioned_sdk]": '"joey"',
                "params[cui_gk]": '"[PASS]:jssdk,read"',
                "source": "gdp_delegated",
                "cache_buster": str(random.randint(-10**18, 10**18)),
            }
        )
        gdp_url = f"https://www.facebook.com/privacy/consent/?{gdp_query}"
        _debug_log(f"fallback GDP -> {gdp_url[:100]}...", debug=debug)
        response = session.get(gdp_url, headers={"referer": DOLA_HOME})
        final_url = str(response.url or gdp_url)
        html = response.text or ""
    if "/privacy/consent/" not in final_url:
        raise RuntimeError(f"Expected GDP consent page, got {urlparse(final_url).path or final_url[:120]}")
    actor_id = session.cookies.get("c_user") or ""
    ctx = scrape_fb_page_context(html, actor_id=actor_id)
    if not ctx.get("fb_dtsg") or not ctx.get("lsd"):
        raise RuntimeError("GDP page did not expose fb_dtsg/lsd (session may be invalid).")
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
    mutate("validation", sahara_input(**base, client_mutation_id="3"))
    outcome_payload = mutate("outcome", sahara_input(**base, client_mutation_id="4"))
    complete_url = extract_consent_complete_url(outcome_payload)
    if not complete_url:
        raise RuntimeError("PostPromptOutcome did not return consent/complete URL.")
    _debug_log(f"  consent/complete URL captured ({len(complete_url)} chars)", debug=debug)
    return complete_url


def harvest_one(facebook_cookies: dict[str, str], *, timeout_seconds: int = 90, debug: bool = False) -> dict[str, Any]:
    fb_session = make_session(timeout_seconds)
    apply_facebook_cookies(fb_session, facebook_cookies)
    minted = mint_facebook_browser_cookies(fb_session)
    if minted:
        print(f"  minted Facebook cookies: {', '.join(minted)}")
    assert_facebook_session(fb_session)

    gdp_url, _html, ctx = fetch_gdp_page(fb_session, debug=debug)
    complete_url = replay_sahara_consent(fb_session, gdp_url, ctx, debug=debug)
    access_token = follow_for_access_token(fb_session, complete_url, referer=gdp_url, debug=debug)
    if not access_token:
        raise RuntimeError("consent/complete did not yield access_token.")

    openid = facebook_openid(fb_session, access_token)
    if not openid:
        raise RuntimeError("Could not resolve Facebook openid from access_token.")

    dola_session = make_session(timeout_seconds)
    bootstrap_dola(dola_session)
    cookies = filter_dola_cookies(
        dola_passport_login(dola_session, access_token=access_token, openid=openid, debug=debug)
    )
    if not has_dola_auth(cookies):
        raise RuntimeError("Dola auth cookies missing after passport login.")
    return {"cookies": cookies, "strategy": "harvest_via_http"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest Dola cookies from Facebook cookies (HTTP only).")
    parser.add_argument("--file", type=Path, default=DEFAULT_INPUT, help="Facebook cookie header file.")
    parser.add_argument("--cookies", default="", help="Single Facebook cookie header string.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output folder.")
    parser.add_argument("--timeout", type=int, default=90, help="HTTP timeout in seconds.")
    parser.add_argument("--debug", action="store_true", help="Print HTTP replay steps.")
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


def _ledger_error(output: Path, stamp: str, c_user: str, status: str, error: str) -> None:
    append_ledger_row(
        output,
        {
            "timestamp": stamp,
            "facebook_c_user": c_user,
            "status": status,
            "dola_auth": "no",
            "cookie_names": "",
            "cookie_header": "",
            "json_path": "",
            "error": error,
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = load_rows(args)
    if not rows:
        print(f"No Facebook cookies found. Copy fb_cookies.example.txt to {args.file} and paste your cookie header.", file=sys.stderr)
        return 2

    failures = 0
    for index, facebook_cookies in enumerate(rows, start=1):
        c_user = facebook_c_user(facebook_cookies)
        stamp = utc_stamp()
        recommended = [name for name in RECOMMENDED_FACEBOOK_COOKIES if name not in facebook_cookies and name != "sb"]
        missing = missing_facebook_cookies(facebook_cookies)
        print(f"[{index}/{len(rows)}] Facebook c_user={c_user or '?'}")
        if recommended:
            print(f"  missing recommended cookies: {', '.join(recommended)}")
        if missing:
            print(f"  skipped, missing {', '.join(missing)}", file=sys.stderr)
            _ledger_error(args.output, stamp, c_user, "skipped", f"missing {', '.join(missing)}")
            failures += 1
            continue
        try:
            result = harvest_one(facebook_cookies, timeout_seconds=args.timeout, debug=args.debug)
            cookies = result["cookies"]
            paths = write_profile_files(args.output, facebook_c_user_id=c_user, cookies=cookies, stamp=stamp)
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
            print(f"  saved {paths['json'].name} via {result['strategy']} ({len(cookies)} Dola cookies)")
        except Exception as exc:
            failures += 1
            _ledger_error(args.output, stamp, c_user, "error", str(exc))
            print(f"  failed: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

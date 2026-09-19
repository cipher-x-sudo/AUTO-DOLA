from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from curl_cffi import requests as curl_requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


AUTH_COOKIE_PATHS = (
    Path("/run/secrets/dola_auth_cookies"),
    Path("/data/auth_cookies.txt"),
    Path("backend/auth_cookies.txt"),
    Path("auth_cookies.txt"),
)

ANDROID_WEBVIEW_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S928B Build/UP1A.231005.007; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36"
)
NEXUS_CHROME_IMPERSONATE = "chrome110"
PAYLOAD_TEMPLATE_VERSION = "nexus-anonymous-2026-06-25"
VIDEO_POLL_ATTEMPTS = 250
VIDEO_POLL_INTERVAL_SECONDS = 5
PLAY_INFO_POLL_ATTEMPTS = 200
PLAY_INFO_POLL_INTERVAL_SECONDS = 5
QAAB_SALT = bytes.fromhex(
    "4dd4c2e6b83162090e52b3c7a6733ba4"
    "1cb2462b829ab58a196b39db57177524"
    "f49baf7f08e8d68d26a72e37c1a95a2f"
    "1f05a51892aef2949732b62a38aadd58"
)
VIDEO_FAILURE_MARKERS = (
    "failed to generate",
    "generation failed",
    "unable to generate",
    "cannot generate",
    "can't generate",
    "cant generate",
    "couldn't generate",
    "could not generate",
    "content you requested",
    "no points",
    "insufficient points",
    "high demand",
    "rate limit",
    "limit reached",
    "try again later",
    "may violate our policies",
    "violate our policies",
    "violates our policies",
    "policy violation",
    "modify it and try again",
    "input contains content",
    "safety policy",
    "content policy",
    "violates",
    "inappropriate",
    "system error",
)


@dataclass
class DolaSession:
    url: str
    headers: dict[str, str]
    payload_template: dict[str, Any]
    fp: str
    has_ttwid: bool
    has_hook_slardar: bool
    has_auth_cookies: bool
    unwatermarked_url: str = ""
    seen_fallback_apis: set[str] = field(default_factory=set)


@dataclass
class DolaSubmitResult:
    conversation_id: str
    conversation_type: int
    assistant_messages: list[str]


class DolaSubmissionError(RuntimeError):
    def __init__(self, message: str, diagnostic: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class DolaTerminalGenerationError(RuntimeError):
    """A non-retryable Dola response for a specific prompt."""


class DolaClient:
    def __init__(self, auth_cookies: str = "", region: str = "BD", timeout: float = 30, proxy: str = "") -> None:
        self.auth_cookies = auth_cookies.strip()
        self.region = region
        self.timeout = timeout
        self.proxy = proxy.strip() or None
        self._http: curl_requests.AsyncSession | None = None

    async def aclose(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None

    def _session(self) -> curl_requests.AsyncSession:
        if self._http is None:
            self._http = curl_requests.AsyncSession(
                timeout=self.timeout,
                verify=False,
                impersonate=NEXUS_CHROME_IMPERSONATE,
                proxy=self.proxy,
            )
        return self._http

    async def build_session(self) -> DolaSession:
        device_id = str(uuid.uuid4().int)[:19]
        tea_uuid = str(uuid.uuid4().int)[:19]
        web_tab_id = str(uuid.uuid4())
        fp = generate_fp()
        url = (
            "https://www.dola.com/chat/completion"
            f"?aid=495671&device_id={device_id}&device_platform=android&fp={fp}"
            f"&language=en&pc_version=3.23.5&pkg_type=release_version&real_aid=495671"
            f"&region={self.region}&samantha_web=1&sys_region={self.region}&tea_uuid={tea_uuid}"
            f"&use-olympus-account=1&version_code=20800&web_id={tea_uuid}"
            f"&web_platform=web&web_tab_id={web_tab_id}"
        )
        public_cookies = await self._fetch_public_cookies()
        if not public_cookies.get("ttwid"):
            raise RuntimeError("Public Dola session failed: no ttwid cookie.")
        auth_cookies = read_auth_cookies(self.auth_cookies)
        merged_cookies = merge_cookies(
            {"i18next": "en", "flow_user_country": self.region, "s_v_web_id": fp},
            auth_cookies,
            public_cookies,
        )
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "agw-js-conv": "str, str",
            "content-type": "application/json",
            "cookie": format_cookie_header(merged_cookies),
            "last-event-id": "undefined",
            "origin": "https://www.dola.com",
            "referer": "https://www.dola.com/chat/",
            "user-agent": ANDROID_WEBVIEW_UA,
            "sec-ch-ua": '"Chromium";v="130", "Android WebView";v="130", "Not?A_Brand";v="99"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "x-requested-with": "com.android.browser",
        }
        return DolaSession(
            url=url,
            headers=headers,
            payload_template=base_payload(fp),
            fp=fp,
            has_ttwid=True,
            has_hook_slardar=bool(public_cookies.get("hook_slardar_session_id")),
            has_auth_cookies=bool(auth_cookies),
        )

    async def _fetch_public_cookies(self) -> dict[str, str]:
        try:
            headers = {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
                "user-agent": ANDROID_WEBVIEW_UA,
                "sec-ch-ua": '"Chromium";v="130", "Android WebView";v="130", "Not?A_Brand";v="99"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "upgrade-insecure-requests": "1",
            }
            response = await self._session().get("https://www.dola.com/", headers=headers, allow_redirects=True, timeout=15)
            cookies = parse_set_cookie_headers(extract_set_cookie_headers(response.headers))
            cookies.update({key: value for key, value in response.cookies.items()})
            cookies.update({key: value for key, value in self._session().cookies.items()})
            return cookies
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to fetch public Dola cookies: %s", exc)
        return {}

    async def submit(
        self,
        session: DolaSession,
        payload: dict[str, Any],
        *,
        raw_response_fn: Callable[[str, int, int, str], None] | None = None,
        attempt: int = 1,
    ) -> DolaSubmitResult:
        response = await self._session().post(session.url, headers=session.headers, json=payload)
        if raw_response_fn:
            raw_response_fn("submit", attempt, response.status_code, response.text)
        return parse_submit_response(session, payload, response)

    async def poll_video_id(
        self,
        session: DolaSession,
        conversation_id: str,
        conversation_type: int,
        *,
        max_attempts: int = VIDEO_POLL_ATTEMPTS,
        sleep_seconds: float = VIDEO_POLL_INTERVAL_SECONDS,
        log_fn: Callable[[str, str], None] | None = None,
        raw_response_fn: Callable[[str, int, int, str], None] | None = None,
        cancel_fn: Callable[[], bool] | None = None,
        assistant_message_fn: Callable[[str], Any] | None = None,
    ) -> str | None:
        url = session.url.replace("chat/completion", "im/chain/single")
        headers = {k: v for k, v in session.headers.items() if k.lower() not in {"content-type", "accept-encoding", "agw-js-conv"}}
        headers["content-type"] = "application/json; encoding=utf-8"
        headers["agw-js-conv"] = "str"
        body = build_chain_poll_body(conversation_id, conversation_type)
        seen_messages: set[str] = set()
        for attempt in range(1, max_attempts + 1):
            if cancel_fn and cancel_fn():
                if log_fn:
                    log_fn("Video polling cancelled.", "warn")
                return None
            if log_fn:
                log_fn(f"Polling video id {attempt}/{max_attempts}", "info")
            body["sequence_id"] = str(uuid.uuid4())
            response = await self._session().post(url, headers=headers, json=body)
            if raw_response_fn:
                raw_response_fn("chain_poll", attempt, response.status_code, response.text)
            if response.status_code == 200:
                payload = response.json()
                await self._capture_unwatermarked_url(session, payload, response.text, log_fn=log_fn)
                vid, _ = parse_vid_with_diagnostics(payload)
                if vid:
                    if log_fn:
                        log_fn("Dola returned video id.", "success")
                    return vid

                replacement_conversation: tuple[str, int] | None = None
                for message in extract_chain_texts(payload):
                    if message not in seen_messages:
                        seen_messages.add(message)
                        if log_fn:
                            level = "warn" if is_terminal_video_failure(message) else "info"
                            log_fn(message[:500], level)
                        if assistant_message_fn:
                            replacement = assistant_message_fn(message)
                            if inspect.isawaitable(replacement):
                                replacement = await replacement
                            if replacement:
                                replacement_conversation = replacement
                                break
                    if is_terminal_video_failure(message):
                        raise DolaTerminalGenerationError(f"Dola rejected this prompt: {message[:500]}")
                if replacement_conversation:
                    conversation_id, conversation_type = replacement_conversation
                    body = build_chain_poll_body(conversation_id, conversation_type)
                    seen_messages.clear()
                    # Poll the replacement request immediately; it is the
                    # automatic confirmation/retry for unsupported duration.
                    continue
            elif log_fn and attempt == 1:
                log_fn(f"Dola chain poll returned HTTP {response.status_code}.", "warn")
            await asyncio.sleep(sleep_seconds)
        return None

    async def poll_download_url(
        self,
        session: DolaSession,
        vid: str,
        *,
        raw_response_fn: Callable[[str, int, int, str], None] | None = None,
        cancel_fn: Callable[[], bool] | None = None,
        log_fn: Callable[[str, str], None] | None = None,
        max_attempts: int = PLAY_INFO_POLL_ATTEMPTS,
        sleep_seconds: float = PLAY_INFO_POLL_INTERVAL_SECONDS,
    ) -> str | None:
        if session.unwatermarked_url:
            if log_fn:
                log_fn("Using raw unwatermarked Dola source from fallback_api.", "success")
            return session.unwatermarked_url
        url = session.url.replace("chat/completion", "samantha/video/get_play_info")
        headers = {k: v for k, v in session.headers.items() if k.lower() not in {"content-type", "accept-encoding"}}
        headers["content-type"] = "application/json"
        for attempt in range(1, max_attempts + 1):
            if cancel_fn and cancel_fn():
                if log_fn:
                    log_fn("Download URL polling cancelled.", "warn")
                return None
            if log_fn:
                log_fn(f"Polling play_info {attempt}/{max_attempts}", "info")
            response = await self._session().post(url, headers=headers, json={"vid": vid})
            if raw_response_fn:
                raw_response_fn("play_info", attempt, response.status_code, response.text)
            if response.status_code == 200:
                payload = response.json()
                await self._capture_unwatermarked_url(session, payload, response.text, log_fn=log_fn)
                download_url = session.unwatermarked_url or parse_play_info(payload)
                if download_url:
                    return download_url
            for _ in range(max(1, int(sleep_seconds / 0.5))):
                if cancel_fn and cancel_fn():
                    if log_fn:
                        log_fn("Download URL polling cancelled.", "warn")
                    return None
                await asyncio.sleep(0.5)
        return None

    async def _capture_unwatermarked_url(
        self,
        session: DolaSession,
        payload: Any,
        raw_body: str = "",
        *,
        log_fn: Callable[[str, str], None] | None = None,
    ) -> str:
        for fallback_api in find_dola_fallback_apis(payload, raw_body):
            if fallback_api in session.seen_fallback_apis:
                continue
            session.seen_fallback_apis.add(fallback_api)
            if log_fn:
                log_fn("Found Dola fallback_api; requesting raw unwatermarked source.", "info")
            try:
                api_url = build_unwatermarked_fallback_url(fallback_api)
                response = await self._session().get(
                    api_url,
                    headers={
                        "accept": "application/json,text/plain,*/*",
                        "cookie": "",
                        "user-agent": ANDROID_WEBVIEW_UA,
                    },
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    if log_fn:
                        log_fn(f"Unwatermarked fallback returned HTTP {response.status_code}; using normal play_info fallback.", "warn")
                    continue
                raw_url = parse_unwatermarked_video_url(response.json())
                if raw_url:
                    session.unwatermarked_url = raw_url
                    if log_fn:
                        log_fn("Resolved raw unwatermarked Dola video source.", "success")
                    return raw_url
            except Exception as exc:
                if log_fn:
                    log_fn(f"Unwatermarked fallback could not be resolved ({type(exc).__name__}); using normal play_info fallback.", "warn")
        return ""


def parse_submit_response(session: DolaSession, payload: dict[str, Any], response: Any) -> DolaSubmitResult:
    diagnostic = build_submit_diagnostic(session, payload, response)
    try:
        response.raise_for_status()
        conversation_id, conversation_type = parse_conversation_from_stream(response.text)
        return DolaSubmitResult(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            assistant_messages=parse_assistant_messages_from_stream(response.text),
        )
    except Exception as exc:
        message = "Dola rejected anonymous session."
        if "common invalid param" in response.text.lower():
            message = "Dola rejected the request payload/session: common invalid param."
        elif diagnostic.get("error_code") == 710022002:
            message = "Dola is experiencing high demand. Please try again later."
        elif diagnostic.get("error_code") == 710022017:
            message = "Dola country/region restricted this session."
        elif "Could not parse conversation_id" in str(exc):
            message = "Dola did not return conversation_id."
        elif response.status_code in {401, 403, 429}:
            message = f"Dola rejected anonymous session: HTTP {response.status_code}."
        raise DolaSubmissionError(message, diagnostic) from exc


def generate_fp() -> str:
    return f"verify_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:12]}"


def base_payload(fp: str | None = None) -> dict[str, Any]:
    fp = fp or generate_fp()
    now_ms = int(time.time() * 1000)
    now_sec = int(time.time())
    return {
        "client_meta": {
            "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
            "conversation_id": "",
            "bot_id": "7339470689562525703",
            "last_section_id": "",
            "last_message_index": None,
        },
        "messages": [
            {
                "local_message_id": str(uuid.uuid4()),
                "content_block": [
                    {
                        "block_type": 10000,
                        "content": {
                            "text_block": {
                                "text": "placeholder",
                                "icon_url": "",
                                "icon_url_dark": "",
                                "summary": "",
                            },
                            "pc_event_block": "",
                        },
                        "block_id": str(uuid.uuid4()),
                        "parent_id": "",
                        "meta_info": [],
                        "append_fields": [],
                    }
                ],
                "message_status": 0,
            }
        ],
        "option": {
            "send_message_scene": "",
            "create_time_ms": now_ms,
            "collect_id": "",
            "is_audio": False,
            "answer_with_suggest": False,
            "tts_switch": False,
            "need_deep_think": 0,
            "click_clear_context": False,
            "from_suggest": False,
            "is_regen": False,
            "is_replace": False,
            "is_from_click_option": False,
            "disable_sse_cache": False,
            "select_text_action": "",
            "is_select_text": False,
            "resend_for_regen": False,
            "scene_type": 0,
            "unique_key": str(uuid.uuid4()),
            "start_seq": 0,
            "need_create_conversation": True,
            "conversation_init_option": {"need_ack_conversation": True},
            "regen_query_id": [],
            "edit_query_id": [],
            "regen_instruction": "",
            "no_replace_for_regen": False,
            "message_from": 0,
            "shared_app_name": "",
            "shared_app_id": "",
            "sse_recv_event_options": {"support_chunk_delta": True},
            "is_ai_playground": False,
            "is_old_user": False,
            "recovery_option": {
                "is_recovery": False,
                "req_create_time_sec": now_sec,
                "append_sse_event_scene": 0,
            },
            "message_storage_type": 0,
        },
        "user_context": [],
        "ext": {
            "use_deep_think": "0",
            "fp": fp,
            "sub_conv_firstmet_type": "1",
            "collection_id": "",
            "conversation_init_option": '{"need_ack_conversation":true}',
            "commerce_credit_config_enable": "0",
        },
    }


def parse_cookie_text_with_stats(text: str) -> tuple[dict[str, str], int]:
    cookies: dict[str, str] = {}
    malformed = 0
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        if cleaned.lower().startswith("cookie:"):
            cleaned = cleaned.split(":", 1)[1].strip()
        for part in cleaned.split(";"):
            if not part.strip():
                continue
            if "=" not in part:
                malformed += 1
                continue
            key, value = part.strip().split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                cookies[key] = value
            else:
                malformed += 1
    return cookies, malformed


def parse_cookie_text(text: str) -> dict[str, str]:
    cookies, _ = parse_cookie_text_with_stats(text)
    return cookies


def read_auth_cookies(settings_cookies: str = "", paths: tuple[Path, ...] = AUTH_COOKIE_PATHS) -> dict[str, str]:
    configured = parse_cookie_text(settings_cookies)
    if configured:
        return configured
    for path in paths:
        try:
            if path.exists() and path.is_file():
                cookies = parse_cookie_text(path.read_text(encoding="utf-8"))
                if cookies:
                    return cookies
        except OSError:
            continue
    return parse_cookie_text(settings_cookies)


def parse_set_cookie_headers(headers: list[str]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for header in headers:
        first = header.split(";", 1)[0].strip()
        if "=" in first:
            key, value = first.split("=", 1)
            cookies[key] = value
    return cookies


def extract_set_cookie_headers(headers: Any) -> list[str]:
    if hasattr(headers, "get_list"):
        return list(headers.get_list("set-cookie"))
    if hasattr(headers, "get_all"):
        return list(headers.get_all("set-cookie"))
    value = headers.get("set-cookie", "") if hasattr(headers, "get") else ""
    if not value:
        return []
    return [value]


def merge_cookies(*sources: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in sources:
        for key, value in source.items():
            if value:
                merged[key] = value
    return merged


def format_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def cookie_names_from_header(cookie_header: str) -> list[str]:
    return [part.split("=", 1)[0].strip() for part in cookie_header.split(";") if "=" in part]


def parse_dola_stream_error(text: str) -> tuple[int | None, str]:
    code_match = re.search(r'"(?:error_code|code)"\s*:\s*([0-9]+)', text)
    message_match = re.search(r'"(?:error_msg|message|msg)"\s*:\s*"([^"]+)"', text)
    return (int(code_match.group(1)) if code_match else None, message_match.group(1) if message_match else "")


def build_submit_diagnostic(session: DolaSession, payload: dict[str, Any], response: Any) -> dict[str, Any]:
    ability = payload.get("chat_ability", {})
    try:
        ability_param = json.loads(ability.get("ability_param", "{}"))
    except (TypeError, json.JSONDecodeError):
        ability_param = {}
    text = payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"]
    cookie_names = cookie_names_from_header(session.headers.get("cookie", ""))
    query = parse_qs(urlparse(session.url).query)
    query_keys = sorted(query.keys())
    error_code, error_msg = parse_dola_stream_error(response.text)
    return {
        "status_code": response.status_code,
        "has_ttwid": session.has_ttwid,
        "has_hook_slardar": session.has_hook_slardar,
        "has_auth_cookies": session.has_auth_cookies,
        "cookie_count": len(cookie_names),
        "cookie_names": cookie_names,
        "payload_template_version": PAYLOAD_TEMPLATE_VERSION,
        "option_key_count": len(payload.get("option", {})),
        "has_ext_fp": bool(payload.get("ext", {}).get("fp")),
        "fp_matches_url": payload.get("ext", {}).get("fp") == query.get("fp", [""])[0] == session.fp,
        "fp_matches_cookie": f"s_v_web_id={session.fp}" in session.headers.get("cookie", ""),
        "url_query_keys": query_keys,
        "url_has_web_platform": query.get("web_platform", [""])[0] == "web",
        "model": ability_param.get("model"),
        "duration": ability_param.get("duration"),
        "prompt_text": sanitize_dola_log_message(text)[:300],
        "error_code": error_code,
        "error_msg": error_msg,
        "body_snippet": sanitize_dola_log_message(response.text.replace("\n", " ")),
    }


def format_diagnostic(diagnostic: dict[str, Any]) -> str:
    return (
        "Dola diagnostic: "
        f"status={diagnostic.get('status_code')}, "
        f"ttwid={diagnostic.get('has_ttwid')}, "
        f"hook_slardar={diagnostic.get('has_hook_slardar')}, "
        f"auth_cookies={diagnostic.get('has_auth_cookies')}, "
        f"cookie_count={diagnostic.get('cookie_count')}, "
        f"cookie_names={diagnostic.get('cookie_names')}, "
        f"payload_template={diagnostic.get('payload_template_version')}, "
        f"option_keys={diagnostic.get('option_key_count')}, "
        f"ext_fp={diagnostic.get('has_ext_fp')}, "
        f"fp_matches_url={diagnostic.get('fp_matches_url')}, "
        f"fp_matches_cookie={diagnostic.get('fp_matches_cookie')}, "
        f"url_query_keys={diagnostic.get('url_query_keys')}, "
        f"web_platform={diagnostic.get('url_has_web_platform')}, "
        f"model={diagnostic.get('model')}, "
        f"duration={diagnostic.get('duration')}, "
        f"prompt={diagnostic.get('prompt_text')}, "
        f"error_code={diagnostic.get('error_code')}, "
        f"error_msg={diagnostic.get('error_msg')}, "
        f"body={diagnostic.get('body_snippet')}"
    )


async def dola_session_status(auth_cookies: str = "", region: str = "BD") -> dict[str, Any]:
    client = DolaClient(auth_cookies=auth_cookies, region=region)
    try:
        session = await client.build_session()
        return {
            "ok": True,
            "has_ttwid": session.has_ttwid,
            "has_hook_slardar": session.has_hook_slardar,
            "has_auth_cookies": session.has_auth_cookies,
            "region": region,
        }
    except Exception as exc:
        return {"ok": False, "has_ttwid": False, "has_auth_cookies": False, "region": region, "error": str(exc)}


def build_dola_payload(
    template: dict[str, Any],
    prompt: str,
    duration: int,
    ratio: str,
    model: str = "seedance_v2.0",
) -> dict[str, Any]:
    payload = json.loads(json.dumps(template))
    duration = int(duration)
    ratio = str(ratio or "9:16")
    model = normalize_video_model(model)
    payload["messages"][0]["local_message_id"] = str(uuid.uuid4())
    payload["messages"][0]["content_block"][0]["block_id"] = str(uuid.uuid4())
    payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"] = build_video_prompt_text(prompt, duration, ratio)
    payload["option"]["unique_key"] = str(uuid.uuid4())
    payload["option"]["create_time_ms"] = int(time.time() * 1000)
    payload["option"]["recovery_option"]["req_create_time_sec"] = int(time.time())
    payload["chat_ability"] = {"ability_type": 17, "ability_param": json.dumps({"model": model, "duration": duration, "ratio": ratio}, separators=(",", ":"))}
    payload["client_meta"]["conversation_id"] = ""
    payload["client_meta"]["last_section_id"] = ""
    payload["client_meta"]["last_message_index"] = None
    payload["client_meta"]["local_conversation_id"] = f"local_{uuid.uuid4().int % 10000000000000000}"
    payload["option"]["need_create_conversation"] = True
    payload["option"]["conversation_init_option"] = {"need_ack_conversation": True}
    payload["ext"]["conversation_init_option"] = '{"need_ack_conversation":true}'
    return payload


def normalize_video_model(model: str) -> str:
    normalized = str(model or "seedance_v2.0").strip().lower().replace(" ", "_")
    aliases = {
        "seedance_2.0": "seedance_v2.0",
        "seedance_2.5": "seedance_v2.5",
        "seedance_v2": "seedance_v2.0",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"seedance_v2.0", "seedance_v2.5"}:
        raise ValueError(f"Unsupported Dola video model: {model}.")
    return normalized


def build_video_prompt_text(prompt: str, duration: int, ratio: str) -> str:
    clean_prompt = re.sub(r"(?i)^generate\s+video\s*:\s*", "", prompt.strip()).strip()
    return f"Generate video: {clean_prompt}"


def build_chain_poll_body(conversation_id: str, conversation_type: int) -> dict[str, Any]:
    return {
        "cmd": 3100,
        "uplink_body": {
            "pull_singe_chain_uplink_body": {
                "conversation_id": conversation_id,
                "anchor_index": 0,
                "conversation_type": conversation_type,
                "direction": 3,
                "limit": 50,
                "ext": {},
                "filter": {"index_list": []},
                "evaluate_ab_params": "",
                "evaluate_common_params": "",
            }
        },
        "sequence_id": str(uuid.uuid4()),
        "channel": 2,
        "version": "1",
    }


def parse_conversation_from_stream(text: str) -> tuple[str, int]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([0-9]+)"', text)
    if not conversation_match:
        error_match = re.search(r'"(?:error_msg|message|msg)"\s*:\s*"([^"]+)"', text)
        if error_match:
            raise ValueError(error_match.group(1))
        raise ValueError(f"Could not parse conversation_id from stream response: {text[:800].replace(chr(10), ' ')}")
    type_match = re.search(r'"conversation_info"\s*:\s*\{[^}]*?"conversation_type"\s*:\s*([0-9]+)', text)
    if type_match:
        return conversation_match.group(1), int(type_match.group(1))
    all_types = re.findall(r'"conversation_type"\s*:\s*([0-9]+)', text)
    return conversation_match.group(1), int(all_types[-1]) if all_types else 3


def parse_assistant_messages_from_stream(text: str) -> list[str]:
    messages: list[str] = []
    seen: set[str] = set()
    for chunk in parse_sse_data_chunks(text):
        for extracted in _extract_text_values(chunk):
            cleaned = sanitize_dola_log_message(normalize_dola_message(extracted))
            if is_assistant_log_message(cleaned) and cleaned not in seen:
                seen.add(cleaned)
                messages.append(cleaned)
    return messages


def parse_sse_data_chunks(text: str) -> list[Any]:
    chunks: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        data = stripped.split(":", 1)[1].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunks.append(json.loads(data))
        except json.JSONDecodeError:
            chunks.append(data)
    if not chunks:
        try:
            chunks.append(json.loads(text))
        except json.JSONDecodeError as exc:
            import logging
            logging.getLogger(__name__).warning("Could not parse Dola response as JSON: %s", exc)
    return chunks


def normalize_dola_message(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    compacted: list[str] = []
    previous_blank = False
    for line in lines:
        if line:
            compacted.append(line)
            previous_blank = False
        elif not previous_blank:
            compacted.append("")
            previous_blank = True
    return "\n".join(compacted)


def sanitize_dola_log_message(text: str) -> str:
    return re.sub(r"https?://\S+", "[redacted-url]", text)


def is_assistant_log_message(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    blocked_fragments = (
        "generate video:",
        "generate exactly ",
        "conversation_id",
        "conversation_type",
        "local_message_id",
        "cookie",
        "authorization",
        "bearer ",
    )
    return not any(fragment in lowered for fragment in blocked_fragments)


def suggested_video_duration_from_message(message: str, requested_duration: int) -> int | None:
    """Return Dola's suggested duration when it asks to downgrade a request."""
    if int(requested_duration) <= 15:
        return None
    lowered = str(message or "").lower()
    if "supports duration" not in lowered or "nearest supported duration" not in lowered:
        return None
    nearest = re.search(r"nearest supported duration\s*(?:of)?\s*(\d+)\s*seconds?", lowered)
    if nearest:
        value = int(nearest.group(1))
        return value if 1 <= value <= 15 else None
    supported = re.search(r"supports durations?\s+from\s+(\d+)\s+to\s+(\d+)\s*seconds?", lowered)
    if supported:
        value = int(supported.group(2))
        return value if 1 <= value <= 15 else None
    return None


def parse_vid(payload: dict[str, Any]) -> str | None:
    vid, _ = parse_vid_with_diagnostics(payload)
    return vid


def parse_vid_with_diagnostics(payload: dict[str, Any]) -> tuple[str | None, list[str]]:
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"Dola API error code {payload.get('code')}: {payload.get('message', 'Unknown error')}")
    checked_paths = ["full JSON string"]
    match = re.search(r'"vid"\s*:\s*"([a-zA-Z0-9_:-]+)"', json.dumps(payload))
    if match:
        return match.group(1), checked_paths

    known_paths = (
        ("data.pull_singe_chain_uplink_body.messages", _get_path(payload, ("data", "pull_singe_chain_uplink_body", "messages"))),
        (
            "downlink_body.pull_singe_chain_downlink_body.messages",
            _get_path(payload, ("downlink_body", "pull_singe_chain_downlink_body", "messages")),
        ),
        (
            "data.downlink_body.pull_singe_chain_downlink_body.messages",
            _get_path(payload, ("data", "downlink_body", "pull_singe_chain_downlink_body", "messages")),
        ),
    )
    for path, value in known_paths:
        checked_paths.append(path)
        vid = _find_vid_recursive(value)
        if vid:
            return vid, checked_paths

    checked_paths.append("recursive JSON/string scan")
    return _find_vid_recursive(payload), checked_paths


def _get_path(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _find_vid_recursive(value: Any) -> str | None:
    if isinstance(value, str):
        match = re.search(r'"vid"\s*:\s*"([a-zA-Z0-9_:-]+)"', value)
        if match:
            return match.group(1)
        try:
            return _find_vid_recursive(json.loads(value))
        except json.JSONDecodeError:
            return None
    if isinstance(value, list):
        for item in value:
            vid = _find_vid_recursive(item)
            if vid:
                return vid
    if isinstance(value, dict):
        vid_value = value.get("vid")
        if isinstance(vid_value, str) and vid_value:
            return vid_value
        for child in value.values():
            vid = _find_vid_recursive(child)
            if vid:
                return vid
    return None


def extract_chain_texts(payload: dict[str, Any]) -> list[str]:
    messages = _find_message_lists(payload)
    texts: list[str] = []
    seen: set[str] = set()
    for message in messages:
        content = message.get("content") or message.get("message") or message.get("text") or ""
        for text in _extract_text_values(content):
            cleaned = " ".join(text.split())
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                texts.append(cleaned)
    return texts


def _find_message_lists(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list):
            found.extend(message for message in messages if isinstance(message, dict))
        for child in value.values():
            found.extend(_find_message_lists(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_message_lists(child))
    return found


def _extract_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            return _extract_text_values(json.loads(stripped))
        except json.JSONDecodeError:
            return [stripped]
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            texts.extend(_extract_text_values(item))
        return texts
    if isinstance(value, dict):
        texts: list[str] = []
        for key in ("text", "summary", "message", "msg"):
            text = value.get(key)
            if isinstance(text, str):
                texts.append(text)
        text_block = value.get("text_block")
        if isinstance(text_block, dict) and isinstance(text_block.get("text"), str):
            texts.append(text_block["text"])
        for key in ("content", "message", "data", "delta", "answer"):
            content = value.get(key)
            if content is not None and not isinstance(content, str):
                texts.extend(_extract_text_values(content))
            elif key == "content" and isinstance(content, str):
                texts.extend(_extract_text_values(content))
        return texts
    return []


def is_terminal_video_failure(text: str) -> bool:
    lowered = text.lower().replace("’", "'")
    return any(marker in lowered for marker in VIDEO_FAILURE_MARKERS)


def find_dola_fallback_apis(payload: Any, raw_body: str = "") -> list[str]:
    """Return unique fallback_api URLs without exposing them to diagnostics."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        decoded = decode_json_escaped_fragment(value.strip())
        if is_http_url(decoded) and decoded not in seen:
            seen.add(decoded)
            found.append(decoded)

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "fallback_api":
                    add(child)
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, str) and "fallback_api" in value:
            scan_text(value)
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    walk(json.loads(stripped), depth + 1)
                except json.JSONDecodeError:
                    pass

    def scan_text(text: str) -> None:
        patterns = (
            r'fallback_api\\":\\"(.*?)\\"',
            r'"fallback_api"\s*:\s*"((?:\\.|[^"\\])*)"',
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                add(match.group(1))

    walk(payload)
    if raw_body:
        scan_text(raw_body)
    return found


def decode_json_escaped_fragment(value: str) -> str:
    text = str(value)
    for _ in range(3):
        try:
            decoded = json.loads('"' + text.replace('"', '\\"') + '"')
        except (json.JSONDecodeError, TypeError):
            break
        if decoded == text or not isinstance(decoded, str):
            break
        text = decoded
    return text.replace("\\u0026", "&").replace("\\/", "/")


def is_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def build_unwatermarked_fallback_url(fallback_api: str) -> str:
    parsed = urlparse(decode_json_escaped_fragment(fallback_api))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Dola fallback_api is not an HTTP URL.")
    replacements = {"channel": "no", "codec_type": "8", "logo_type": "unwatermarked"}
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in replacements]
    query.extend(replacements.items())
    return urlunparse(parsed._replace(query=urlencode(query)))


def parse_unwatermarked_video_url(payload: Any) -> str | None:
    data = _fallback_video_data(payload)
    picked = _pick_fallback_main_url(data)
    if not picked:
        return None
    return decode_dola_main_url(picked, _find_key_seed(payload)) or None


def _fallback_video_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    video_info = payload.get("video_info") or _get_path(payload, ("data", "video_info")) or payload
    if not isinstance(video_info, dict):
        return {}
    data = video_info.get("data") or video_info
    return data if isinstance(data, dict) else {}


def _pick_fallback_main_url(data: dict[str, Any]) -> str:
    video_list = data.get("video_list")
    entries = list(video_list.values()) if isinstance(video_list, dict) and video_list else [data]
    best_token = ""
    best_score = (-1, -1, -1)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = entry.get("main_url") or entry.get("play_url") or ""
        if not isinstance(token, str) or not token.strip():
            continue
        width = _number(entry.get("vwidth") or entry.get("width"))
        height = _number(entry.get("vheight") or entry.get("height"))
        bitrate = _number(entry.get("bitrate") or entry.get("real_bitrate"))
        file_size = max(
            (_number(entry.get(key)) for key in ("expectedBytes", "file_size", "fileSize", "filesize", "content_length", "contentLength", "video_size", "videoSize")),
            default=0,
        )
        score = (width * height, bitrate, file_size)
        if score > best_score:
            best_token = token.strip()
            best_score = score
    return best_token


def _number(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _find_key_seed(value: Any, depth: int = 0) -> str:
    if depth > 12:
        return ""
    if isinstance(value, dict):
        seed = value.get("key_seed")
        if isinstance(seed, str) and seed.strip():
            return seed.strip()
        for child in value.values():
            hit = _find_key_seed(child, depth + 1)
            if hit:
                return hit
    elif isinstance(value, list):
        for child in value:
            hit = _find_key_seed(child, depth + 1)
            if hit:
                return hit
    return ""


def decode_dola_main_url(token: str, key_seed: str = "") -> str:
    if is_http_url(token):
        return token
    plain = _ascii_url_from_bytes(_base64_decode_loose(token))
    if is_http_url(plain):
        return plain
    if token.startswith("qAAB") and key_seed:
        return _decode_qaab_token(token, key_seed)
    return ""


def _base64_decode_loose(value: str) -> bytes:
    text = str(value or "").strip()
    variants = (
        text,
        text.translate(str.maketrans({"$": "_", "@": "/", "#": "."})),
        text.translate(str.maketrans({"$": "+", "@": "/", "#": "="})),
    )
    for candidate in dict.fromkeys(variants):
        if not candidate:
            continue
        normalized = candidate.replace("-", "+").replace("_", "/")
        normalized += "=" * ((4 - len(normalized) % 4) % 4)
        try:
            return base64.b64decode(normalized, validate=True)
        except (ValueError, base64.binascii.Error):
            continue
    return b""


def _ascii_url_from_bytes(value: bytes) -> str:
    if not value:
        return ""
    if any(byte not in {9, 10, 13} and (byte < 32 or byte > 126) for byte in value):
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _decode_qaab_token(token: str, key_seed: str) -> str:
    data = _base64_decode_loose(token)
    seed = _base64_decode_loose(key_seed)
    if not data or not seed:
        return ""
    digest1 = hashlib.sha512(seed[:32]).digest()
    digest2 = hashlib.sha512(digest1 + QAAB_SALT).digest()
    key = digest2[:16]
    iv = digest2[16:32]
    attempts: list[tuple[bytes, bytes, bytes]] = []
    if data.startswith(b"\xa8\x00\x01\x00"):
        attempts.extend(((data[4:], key, iv), (data[4:], iv, key)))
        if len(data) > 36:
            attempts.extend(((data[36:], key, data[20:36]), (data[36:], key, iv)))
    else:
        attempts.append((data, key, iv))
    for encrypted, attempt_key, attempt_iv in attempts:
        url = _decrypt_aes_cbc_url(encrypted, attempt_key, attempt_iv)
        if url:
            return url
    return ""


def _decrypt_aes_cbc_url(payload: bytes, key: bytes, iv: bytes) -> str:
    if not payload or len(payload) % 16:
        return ""
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(payload) + decryptor.finalize()
    except ValueError:
        return ""
    direct = _ascii_url_from_bytes(plain)
    if is_http_url(direct):
        return direct
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and pad <= len(plain) and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
    url = _ascii_url_from_bytes(plain)
    return url if is_http_url(url) else ""


def parse_play_info(payload: dict[str, Any]) -> str | None:
    if payload.get("code") != 0:
        return None
    try:
        return payload["data"]["play_infos"][0]["main"]
    except (KeyError, IndexError, TypeError):
        return None

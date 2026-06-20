from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx


AUTH_COOKIE_PATHS = (
    Path("/run/secrets/dola_auth_cookies"),
    Path("/data/auth_cookies.txt"),
    Path("backend/auth_cookies.txt"),
    Path("auth_cookies.txt"),
)

ANDROID_WEBVIEW_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-S918B Build/TP1A.220624.014; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/118.0.0.0 Mobile Safari/537.36"
)
PAYLOAD_TEMPLATE_VERSION = "browser-2026-06-19"
VIDEO_POLL_ATTEMPTS = 250
VIDEO_POLL_INTERVAL_SECONDS = 5
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
            raise RuntimeError("Could not establish Dola session: no ttwid cookie.")
        auth_cookies = read_auth_cookies(self.auth_cookies)
        merged_cookies = merge_cookies(
            {"i18next": "en", "flow_user_country": self.region, "s_v_web_id": fp},
            auth_cookies,
            public_cookies,
        )
        headers = {
            "accept": "*/*",
            "agw-js-conv": "str, str",
            "content-type": "application/json",
            "cookie": format_cookie_header(merged_cookies),
            "last-event-id": "undefined",
            "origin": "https://www.dola.com",
            "referer": "https://www.dola.com/chat/",
            "user-agent": ANDROID_WEBVIEW_UA,
            "sec-ch-ua": '"Chromium";v="118", "Android WebView";v="118", "Not=A?Brand";v="99"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
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
                "user-agent": ANDROID_WEBVIEW_UA,
                "sec-ch-ua": '"Chromium";v="118", "Android WebView";v="118", "Not=A?Brand";v="99"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
            }
            async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True, headers=headers, proxy=self.proxy) as client:
                response = await client.get("https://www.dola.com/")
                cookies = parse_set_cookie_headers(response.headers.get_list("set-cookie"))
                cookies.update({key: value for key, value in response.cookies.items()})
                cookies.update({key: value for key, value in client.cookies.items()})
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
        async with httpx.AsyncClient(timeout=self.timeout, verify=False, proxy=self.proxy) as client:
            response = await client.post(session.url, headers=session.headers, json=payload)
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
    ) -> str | None:
        url = session.url.replace("chat/completion", "im/chain/single")
        headers = {k: v for k, v in session.headers.items() if k.lower() not in {"content-type", "accept-encoding", "agw-js-conv"}}
        headers["content-type"] = "application/json; encoding=utf-8"
        headers["agw-js-conv"] = "str"
        body = build_chain_poll_body(conversation_id, conversation_type)
        seen_messages: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout, verify=False, proxy=self.proxy) as client:
            for attempt in range(1, max_attempts + 1):
                if cancel_fn and cancel_fn():
                    if log_fn:
                        log_fn("Video polling cancelled.", "warn")
                    return None
                if log_fn:
                    log_fn(f"Processing video request (attempt {attempt}/{max_attempts})...", "info")
                body["sequence_id"] = str(uuid.uuid4())
                response = await client.post(url, headers=headers, json=body)
                if raw_response_fn:
                    raw_response_fn("chain_poll", attempt, response.status_code, response.text)
                if response.status_code == 200:
                    payload = response.json()
                    vid, _ = parse_vid_with_diagnostics(payload)
                    if vid:
                        if log_fn:
                            log_fn("Dola returned video id.", "success")
                        return vid

                    for message in extract_chain_texts(payload):
                        if message not in seen_messages:
                            seen_messages.add(message)
                            if log_fn:
                                level = "warn" if is_terminal_video_failure(message) else "info"
                                log_fn(message[:500], level)
                        if is_terminal_video_failure(message):
                            raise DolaTerminalGenerationError(f"Dola rejected this prompt: {message[:500]}")
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
    ) -> str | None:
        url = session.url.replace("chat/completion", "samantha/video/get_play_info")
        headers = {k: v for k, v in session.headers.items() if k.lower() not in {"content-type", "accept-encoding"}}
        headers["content-type"] = "application/json"
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            for attempt in range(1, 201):
                if cancel_fn and cancel_fn():
                    if log_fn:
                        log_fn("Download URL polling cancelled.", "warn")
                    return None
                response = await client.post(url, headers=headers, json={"vid": vid})
                if raw_response_fn:
                    raw_response_fn("play_info", attempt, response.status_code, response.text)
                if response.status_code == 200:
                    download_url = parse_play_info(response.json())
                    if download_url:
                        return download_url
                for _ in range(10):
                    if cancel_fn and cancel_fn():
                        if log_fn:
                            log_fn("Download URL polling cancelled.", "warn")
                        return None
                    await asyncio.sleep(0.5)
        return None


def parse_submit_response(session: DolaSession, payload: dict[str, Any], response: httpx.Response) -> DolaSubmitResult:
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
        message = str(exc)
        if "common invalid param" in response.text.lower():
            message = "Dola rejected the request payload/session: common invalid param."
        elif diagnostic.get("error_code") == 710022002:
            message = "Dola is experiencing high demand. Retrying may succeed."
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
    code_match = re.search(r'"error_code"\s*:\s*([0-9]+)', text)
    message_match = re.search(r'"(?:error_msg|message|msg)"\s*:\s*"([^"]+)"', text)
    return (int(code_match.group(1)) if code_match else None, message_match.group(1) if message_match else "")


def build_submit_diagnostic(session: DolaSession, payload: dict[str, Any], response: httpx.Response) -> dict[str, Any]:
    ability = payload.get("chat_ability", {})
    try:
        ability_param = json.loads(ability.get("ability_param", "{}"))
    except (TypeError, json.JSONDecodeError):
        ability_param = {}
    text = payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"]
    ratio = text.rsplit(",", 1)[-1].strip() if "," in text else ""
    cookie_names = cookie_names_from_header(session.headers.get("cookie", ""))
    query = parse_qs(urlparse(session.url).query)
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
        "url_has_web_platform": query.get("web_platform", [""])[0] == "web",
        "model": ability_param.get("model"),
        "duration": ability_param.get("duration"),
        "ratio": ratio,
        "error_code": error_code,
        "error_msg": error_msg,
        "body_snippet": response.text[:500].replace("\n", " "),
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
        f"web_platform={diagnostic.get('url_has_web_platform')}, "
        f"model={diagnostic.get('model')}, "
        f"duration={diagnostic.get('duration')}, "
        f"ratio={diagnostic.get('ratio')}, "
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
        return {"ok": False, "has_ttwid": False, "has_auth_cookies": bool(read_auth_cookies(auth_cookies)), "region": region, "error": str(exc)}


def build_dola_payload(template: dict[str, Any], prompt: str, duration: int, ratio: str) -> dict[str, Any]:
    payload = json.loads(json.dumps(template))
    payload["messages"][0]["local_message_id"] = str(uuid.uuid4())
    payload["messages"][0]["content_block"][0]["block_id"] = str(uuid.uuid4())
    payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"] = f"Generate video: {prompt}, {ratio}"
    payload["option"]["unique_key"] = str(uuid.uuid4())
    payload["option"]["create_time_ms"] = int(time.time() * 1000)
    payload["option"]["recovery_option"]["req_create_time_sec"] = int(time.time())
    payload["chat_ability"] = {"ability_type": 17, "ability_param": json.dumps({"model": "seedance_v2.0", "duration": int(duration)}, separators=(",", ":"))}
    payload["client_meta"]["conversation_id"] = ""
    payload["client_meta"]["last_section_id"] = ""
    payload["client_meta"]["last_message_index"] = None
    payload["client_meta"]["local_conversation_id"] = f"local_{uuid.uuid4().int % 10000000000000000}"
    payload["option"]["need_create_conversation"] = True
    payload["option"]["conversation_init_option"] = {"need_ack_conversation": True}
    payload["ext"]["conversation_init_option"] = '{"need_ack_conversation":true}'
    return payload


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
        "conversation_id",
        "conversation_type",
        "local_message_id",
        "cookie",
        "authorization",
        "bearer ",
    )
    return not any(fragment in lowered for fragment in blocked_fragments)


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


def parse_play_info(payload: dict[str, Any]) -> str | None:
    if payload.get("code") != 0:
        return None
    try:
        return payload["data"]["play_infos"][0]["main"]
    except (KeyError, IndexError, TypeError):
        return None

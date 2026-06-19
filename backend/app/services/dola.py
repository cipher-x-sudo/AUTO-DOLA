from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class DolaSession:
    url: str
    headers: dict[str, str]
    payload_template: dict[str, Any]


class DolaClient:
    def __init__(self, auth_cookies: str = "", region: str = "BD", timeout: float = 30) -> None:
        self.auth_cookies = auth_cookies.strip()
        self.region = region
        self.timeout = timeout

    async def build_session(self) -> DolaSession:
        device_id = str(uuid.uuid4().int)[:19]
        tea_uuid = str(uuid.uuid4().int)[:19]
        web_tab_id = str(uuid.uuid4())
        fp = f"verify_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:12]}"
        url = (
            "https://www.dola.com/chat/completion"
            f"?aid=495671&device_id={device_id}&device_platform=android&fp={fp}"
            f"&language=en&pc_version=3.23.5&pkg_type=release_version&real_aid=495671"
            f"&region={self.region}&samantha_web=1&sys_region={self.region}&tea_uuid={tea_uuid}"
            f"&use-olympus-account=1&version_code=20800&web_id={tea_uuid}&web_tab_id={web_tab_id}"
        )
        ttwid = await self._fetch_ttwid()
        cookies = ["i18next=en", f"flow_user_country={self.region}", f"s_v_web_id={fp}"]
        if ttwid:
            cookies.append(f"ttwid={ttwid}")
        if self.auth_cookies:
            cookies.append(self.auth_cookies)
        headers = {
            "accept": "*/*",
            "agw-js-conv": "str, str",
            "content-type": "application/json",
            "cookie": "; ".join(cookies),
            "origin": "https://www.dola.com",
            "referer": "https://www.dola.com/chat/",
            "user-agent": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 Chrome/118 Mobile Safari/537.36",
            "sec-ch-ua-platform": '"Android"',
        }
        return DolaSession(url=url, headers=headers, payload_template=base_payload())

    async def _fetch_ttwid(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                response = await client.get("https://www.dola.com/")
                for cookie_header in response.headers.get_list("set-cookie"):
                    if "ttwid=" in cookie_header:
                        match = re.match(r"ttwid=([^;]+)", cookie_header)
                        if match:
                            return match.group(1)
        except Exception:
            pass
        return None

    async def submit(self, session: DolaSession, payload: dict[str, Any]) -> tuple[str, int]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            response = await client.post(session.url, headers=session.headers, json=payload)
            response.raise_for_status()
        return parse_conversation_from_stream(response.text)

    async def poll_video_id(self, session: DolaSession, conversation_id: str, conversation_type: int) -> str | None:
        url = session.url.replace("chat/completion", "im/chain/single")
        headers = {k: v for k, v in session.headers.items() if k.lower() not in {"content-type", "accept-encoding", "agw-js-conv"}}
        headers["content-type"] = "application/json; encoding=utf-8"
        headers["agw-js-conv"] = "str"
        body = {
            "cmd": "1",
            "channel": 2,
            "version": "1",
            "sequence_id": str(uuid.uuid4()),
            "uplink_body": {
                "conversation_id": conversation_id,
                "conversation_type": conversation_type,
                "anchor_index": 500,
                "direction": 1,
                "limit": 20,
                "ext": {"pull_single_chain_scene": "multi_device_red_dot_sync"},
                "filter": {"index_list": []},
                "evaluate_ab_params": "",
                "evaluate_common_params": {},
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            for _ in range(250):
                body["sequence_id"] = str(uuid.uuid4())
                response = await client.post(url, headers=headers, json=body)
                if response.status_code == 200:
                    vid = parse_vid(response.json())
                    if vid:
                        return vid
                await asyncio.sleep(5)
        return None

    async def poll_download_url(self, session: DolaSession, vid: str) -> str | None:
        url = session.url.replace("chat/completion", "samantha/video/get_play_info")
        headers = {k: v for k, v in session.headers.items() if k.lower() not in {"content-type", "accept-encoding"}}
        headers["content-type"] = "application/json"
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            for _ in range(200):
                response = await client.post(url, headers=headers, json={"vid": vid})
                if response.status_code == 200:
                    download_url = parse_play_info(response.json())
                    if download_url:
                        return download_url
                await asyncio.sleep(5)
        return None


def base_payload() -> dict[str, Any]:
    return {
        "client_meta": {"local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}", "conversation_id": "", "bot_id": "7339470689562525703", "last_section_id": "", "last_message_index": None},
        "messages": [{"local_message_id": str(uuid.uuid4()), "content_block": [{"block_type": 10000, "content": {"text_block": {"text": "placeholder"}, "pc_event_block": ""}, "block_id": str(uuid.uuid4()), "parent_id": "", "meta_info": [], "append_fields": []}], "message_status": 0}],
        "option": {"send_message_scene": "", "create_time_ms": int(time.time() * 1000), "unique_key": str(uuid.uuid4()), "need_create_conversation": True, "conversation_init_option": {"need_ack_conversation": True}, "recovery_option": {"is_recovery": False, "req_create_time_sec": int(time.time()), "append_sse_event_scene": 0}, "sse_recv_event_options": {"support_chunk_delta": True}},
        "user_context": {"collect_id": "", "is_audio": False, "answer_with_suggest": False, "tts_switch": False},
        "ext": {"conversation_init_option": '{"need_ack_conversation":true}'},
    }


def build_dola_payload(template: dict[str, Any], prompt: str, duration: int, ratio: str) -> dict[str, Any]:
    payload = json.loads(json.dumps(template))
    payload["messages"][0]["local_message_id"] = str(uuid.uuid4())
    payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"] = f"Generate video: {prompt}, {ratio}"
    payload["option"]["unique_key"] = str(uuid.uuid4())
    payload["option"]["create_time_ms"] = int(time.time() * 1000)
    payload["option"]["recovery_option"]["req_create_time_sec"] = int(time.time())
    payload["chat_ability"] = {"ability_type": 17, "ability_param": json.dumps({"model": "seedance_v2.0", "duration": int(duration)})}
    payload["client_meta"]["conversation_id"] = ""
    payload["client_meta"]["last_section_id"] = ""
    payload["client_meta"]["last_message_index"] = None
    payload["client_meta"]["local_conversation_id"] = f"local_{uuid.uuid4().int % 10000000000000000}"
    payload["option"]["need_create_conversation"] = True
    payload["option"]["conversation_init_option"] = {"need_ack_conversation": True}
    payload["ext"]["conversation_init_option"] = '{"need_ack_conversation":true}'
    return payload


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


def parse_vid(payload: dict[str, Any]) -> str | None:
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"Dola API error code {payload.get('code')}: {payload.get('message', 'Unknown error')}")
    match = re.search(r'"vid"\s*:\s*"([a-zA-Z0-9_]+)"', json.dumps(payload))
    return match.group(1) if match else None


def parse_play_info(payload: dict[str, Any]) -> str | None:
    if payload.get("code") != 0:
        return None
    try:
        return payload["data"]["play_infos"][0]["main"]
    except (KeyError, IndexError, TypeError):
        return None

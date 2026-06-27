import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import Request, Response

from app.services.dola import (
    ANDROID_WEBVIEW_UA,
    DolaClient,
    DolaSession,
    DolaSubmissionError,
    PAYLOAD_TEMPLATE_VERSION,
    PLAY_INFO_POLL_ATTEMPTS,
    VIDEO_POLL_ATTEMPTS,
    base_payload,
    build_dola_payload,
    build_chain_poll_body,
    format_cookie_header,
    merge_cookies,
    parse_cookie_text,
    parse_cookie_text_with_stats,
    parse_assistant_messages_from_stream,
    parse_conversation_from_stream,
    parse_play_info,
    parse_submit_response,
    parse_vid,
    parse_vid_with_diagnostics,
    extract_chain_texts,
    is_terminal_video_failure,
    read_auth_cookies,
)
from app.services.raw_responses import split_response_body


class FakeCurlSession:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.cookies: dict[str, str] = {}
        self.posts: list[dict[str, object]] = []

    async def get(self, *_args: object, **_kwargs: object) -> Response:
        return self.responses.pop(0)

    async def post(self, url: str, **kwargs: object) -> Response:
        self.posts.append({"url": url, **kwargs})
        return self.responses.pop(0)


def test_build_payload_includes_seedance_duration_and_prompt() -> None:
    payload = build_dola_payload(base_payload(), "cinematic city flythrough", 15, "9:16")
    assert payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"] == "Generate video: cinematic city flythrough"
    assert payload["chat_ability"]["ability_type"] == 17
    assert json.loads(payload["chat_ability"]["ability_param"]) == {"model": "seedance_v2.0", "duration": 15, "ratio": "9:16"}


def test_base_payload_matches_browser_shape() -> None:
    payload = base_payload("verify_test")
    text_block = payload["messages"][0]["content_block"][0]["content"]["text_block"]

    assert payload["user_context"] == []
    assert text_block == {"text": "placeholder", "icon_url": "", "icon_url_dark": "", "summary": ""}
    assert payload["ext"] == {
        "use_deep_think": "0",
        "fp": "verify_test",
        "sub_conv_firstmet_type": "1",
        "collection_id": "",
        "conversation_init_option": '{"need_ack_conversation":true}',
        "commerce_credit_config_enable": "0",
    }
    assert {
        "collect_id",
        "is_audio",
        "answer_with_suggest",
        "tts_switch",
        "need_deep_think",
        "disable_sse_cache",
        "message_storage_type",
    }.issubset(payload["option"])


def test_chain_poll_body_matches_dola_history_shape() -> None:
    body = build_chain_poll_body("12345", 3)

    assert body["cmd"] == 3100
    assert body["channel"] == 2
    assert body["version"] == "1"
    uplink = body["uplink_body"]["pull_singe_chain_uplink_body"]
    assert uplink == {
        "conversation_id": "12345",
        "anchor_index": 0,
        "conversation_type": 3,
        "direction": 3,
        "limit": 50,
        "ext": {},
        "filter": {"index_list": []},
        "evaluate_ab_params": "",
        "evaluate_common_params": "",
    }


def test_cookie_loader_parses_name_value_lines(tmp_path: Path) -> None:
    path = tmp_path / "auth_cookies.txt"
    path.write_text("sid=abc\n sessionid = xyz \n# ignored\n", encoding="utf-8")

    assert read_auth_cookies(paths=(path,)) == {"sid": "abc", "sessionid": "xyz"}


def test_cookie_loader_parses_raw_cookie_header() -> None:
    assert parse_cookie_text("sid=abc; sessionid=xyz; ttwid=old") == {"sid": "abc", "sessionid": "xyz", "ttwid": "old"}


def test_cookie_loader_counts_malformed_lines() -> None:
    cookies, malformed = parse_cookie_text_with_stats("sid=abc\nbroken-line\nempty=\nCookie: sessionid=xyz; bad")

    assert cookies == {"sid": "abc", "sessionid": "xyz"}
    assert malformed == 3


def test_cookie_loader_missing_file_falls_back_to_settings() -> None:
    assert read_auth_cookies("sid=abc", paths=(Path("missing-auth-cookies.txt"),)) == {"sid": "abc"}


def test_cookie_merge_replaces_stale_ttwid_and_preserves_auth() -> None:
    cookies = merge_cookies({"i18next": "en"}, {"sid": "abc", "ttwid": "old"}, {"ttwid": "fresh"})

    assert cookies["sid"] == "abc"
    assert cookies["ttwid"] == "fresh"
    assert format_cookie_header(cookies) == "i18next=en; sid=abc; ttwid=fresh"


@pytest.mark.asyncio
async def test_build_session_includes_fresh_cookies_and_webview_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_public_cookies(self: DolaClient) -> dict[str, str]:
        return {"ttwid": "fresh", "hook_slardar_session_id": "hook"}

    monkeypatch.setattr(DolaClient, "_fetch_public_cookies", fake_public_cookies)
    session = await DolaClient("sid=abc; ttwid=old").build_session()

    assert "ttwid=fresh" in session.headers["cookie"]
    assert "hook_slardar_session_id=hook" in session.headers["cookie"]
    assert "sid=abc" not in session.headers["cookie"]
    assert "s_v_web_id=verify_" in session.headers["cookie"]
    assert f"s_v_web_id={session.fp}" in session.headers["cookie"]
    assert session.payload_template["ext"]["fp"] == session.fp
    assert parse_qs(urlparse(session.url).query)["fp"] == [session.fp]
    assert parse_qs(urlparse(session.url).query)["web_platform"] == ["web"]
    assert session.headers["user-agent"] == ANDROID_WEBVIEW_UA
    assert session.headers["sec-ch-ua-mobile"] == "?1"
    assert session.has_ttwid is True
    assert session.has_hook_slardar is True
    assert session.has_auth_cookies is False
    query = parse_qs(urlparse(session.url).query)
    assert query["aid"] == ["495671"]
    assert query["real_aid"] == ["495671"]
    assert query["device_platform"] == ["android"]
    assert query["region"] == ["BD"]
    assert query["sys_region"] == ["BD"]
    assert query["samantha_web"] == ["1"]
    assert query["version_code"] == ["20800"]
    assert query["pc_version"] == ["3.23.5"]
    assert query["use-olympus-account"] == ["1"]


@pytest.mark.asyncio
async def test_build_session_requires_ttwid(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_public_cookies(self: DolaClient) -> dict[str, str]:
        return {}

    monkeypatch.setattr(DolaClient, "_fetch_public_cookies", fake_public_cookies)

    with pytest.raises(RuntimeError, match="Public Dola session failed"):
        await DolaClient().build_session()


@pytest.mark.asyncio
async def test_common_invalid_param_has_redacted_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_public_cookies(self: DolaClient) -> dict[str, str]:
        return {"ttwid": "fresh"}

    monkeypatch.setattr(DolaClient, "_fetch_public_cookies", fake_public_cookies)
    client = DolaClient("sid=abc")
    session = await client.build_session()
    payload = build_dola_payload(session.payload_template, "a swimmer", 15, "9:16")

    with pytest.raises(DolaSubmissionError) as exc_info:
        parse_submit_response(session, payload, Response(200, text='{"message":"common invalid param"}'))

    diagnostic = exc_info.value.diagnostic
    assert diagnostic["has_ttwid"] is True
    assert diagnostic["has_hook_slardar"] is False
    assert diagnostic["has_auth_cookies"] is False
    assert diagnostic["cookie_count"] == 4
    assert diagnostic["cookie_names"] == ["i18next", "flow_user_country", "s_v_web_id", "ttwid"]
    assert diagnostic["payload_template_version"] == PAYLOAD_TEMPLATE_VERSION
    assert diagnostic["option_key_count"] >= 30
    assert diagnostic["has_ext_fp"] is True
    assert diagnostic["fp_matches_url"] is True
    assert diagnostic["fp_matches_cookie"] is True
    assert "aid" in diagnostic["url_query_keys"]
    assert diagnostic["url_has_web_platform"] is True
    assert diagnostic["model"] == "seedance_v2.0"
    assert diagnostic["duration"] == 15
    assert diagnostic["prompt_text"] == "Generate video: a swimmer"
    assert "sid=abc" not in diagnostic["body_snippet"]


@pytest.mark.asyncio
async def test_common_invalid_param_reports_payload_session_not_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_public_cookies(self: DolaClient) -> dict[str, str]:
        return {"ttwid": "fresh"}

    monkeypatch.setattr(DolaClient, "_fetch_public_cookies", fake_public_cookies)
    session = await DolaClient().build_session()
    payload = build_dola_payload(session.payload_template, "a swimmer", 15, "9:16")

    with pytest.raises(DolaSubmissionError, match="payload/session"):
        parse_submit_response(session, payload, Response(200, text='event: STREAM_ERROR\ndata: {"error_code":710020202,"error_msg":"common invalid param"}'))


@pytest.mark.asyncio
async def test_high_demand_is_retryable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_public_cookies(self: DolaClient) -> dict[str, str]:
        return {"ttwid": "fresh"}

    monkeypatch.setattr(DolaClient, "_fetch_public_cookies", fake_public_cookies)
    session = await DolaClient().build_session()
    payload = build_dola_payload(session.payload_template, "a swimmer", 15, "9:16")

    with pytest.raises(DolaSubmissionError, match="high demand") as exc_info:
        parse_submit_response(session, payload, Response(200, text='event: STREAM_ERROR\ndata: {"error_code":710022002,"error_msg":"We are experiencing high demand right now. Please try again later."}'))

    assert exc_info.value.diagnostic["error_code"] == 710022002


@pytest.mark.asyncio
async def test_country_restricted_message_uses_top_level_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_public_cookies(self: DolaClient) -> dict[str, str]:
        return {"ttwid": "fresh"}

    monkeypatch.setattr(DolaClient, "_fetch_public_cookies", fake_public_cookies)
    session = await DolaClient().build_session()
    payload = build_dola_payload(session.payload_template, "a swimmer", 10, "9:16")

    with pytest.raises(DolaSubmissionError, match="country/region restricted") as exc_info:
        parse_submit_response(
            session,
            payload,
            Response(200, text='{"code":710022017,"msg":"Cici is not available in your country/region.","message":"country restricted no logout"}'),
        )

    assert exc_info.value.diagnostic["error_code"] == 710022017


@pytest.mark.asyncio
async def test_submit_uses_reusable_curl_session(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DolaClient()
    fake = FakeCurlSession(
        [
            Response(
                200,
                text='data: {"conversation_id":"12345","conversation_info":{"conversation_type":3}}',
                request=Request("POST", "https://www.dola.com/chat/completion"),
            )
        ]
    )
    monkeypatch.setattr(client, "_session", lambda: fake)
    session = DolaSession(
        url="https://www.dola.com/chat/completion?fp=verify_test&web_platform=web",
        headers={"cookie": "i18next=en; flow_user_country=BD; s_v_web_id=verify_test; ttwid=fresh"},
        payload_template={},
        fp="verify_test",
        has_ttwid=True,
        has_hook_slardar=False,
        has_auth_cookies=False,
    )
    payload = build_dola_payload(base_payload("verify_test"), "a swimmer", 10, "9:16")

    result = await client.submit(session, payload)

    assert result.conversation_id == "12345"
    assert fake.posts[0]["url"] == session.url
    assert fake.posts[0]["headers"] == session.headers
    assert fake.posts[0]["json"] == payload


@pytest.mark.asyncio
async def test_poll_video_id_returns_none_when_no_vid(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DolaClient()
    fake = FakeCurlSession(
        [
            Response(
                200,
                json={"code": 0, "data": {"messages": []}},
                request=Request("POST", "https://www.dola.com/im/chain/single"),
            )
        ]
    )
    monkeypatch.setattr(client, "_session", lambda: fake)
    session = DolaSession(
        url="https://www.dola.com/chat/completion?fp=verify_test&web_platform=web",
        headers={"cookie": "i18next=en; flow_user_country=BD; s_v_web_id=verify_test; ttwid=fresh"},
        payload_template={},
        fp="verify_test",
        has_ttwid=True,
        has_hook_slardar=False,
        has_auth_cookies=False,
    )

    logs: list[str] = []

    assert await client.poll_video_id(session, "12345", 3, max_attempts=1, sleep_seconds=0, log_fn=lambda message, _level: logs.append(message)) is None
    assert fake.posts[0]["url"] == "https://www.dola.com/im/chain/single?fp=verify_test&web_platform=web"
    assert logs == ["Polling video id 1/1"]


@pytest.mark.asyncio
async def test_poll_download_url_returns_none_without_play_info(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DolaClient()
    fake = FakeCurlSession(
        [
            Response(
                200,
                json={"code": 0, "data": {"play_infos": []}},
                request=Request("POST", "https://www.dola.com/samantha/video/get_play_info"),
            )
        ]
    )
    monkeypatch.setattr(client, "_session", lambda: fake)
    session = DolaSession(
        url="https://www.dola.com/chat/completion?fp=verify_test&web_platform=web",
        headers={"cookie": "i18next=en; flow_user_country=BD; s_v_web_id=verify_test; ttwid=fresh"},
        payload_template={},
        fp="verify_test",
        has_ttwid=True,
        has_hook_slardar=False,
        has_auth_cookies=False,
    )

    cancel_checks = 0

    def cancel_after_first_response() -> bool:
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks > 1

    logs: list[str] = []

    assert await client.poll_download_url(session, "vid123", cancel_fn=cancel_after_first_response, log_fn=lambda message, _level: logs.append(message)) is None
    assert fake.posts[0]["url"] == "https://www.dola.com/samantha/video/get_play_info?fp=verify_test&web_platform=web"
    assert logs == ["Polling play_info 1/200", "Download URL polling cancelled."]


def test_poll_attempt_constants_match_gui_progress_targets() -> None:
    assert VIDEO_POLL_ATTEMPTS == 250
    assert PLAY_INFO_POLL_ATTEMPTS == 200


def test_parse_conversation_from_stream() -> None:
    text = 'data: {"conversation_id":"12345","conversation_info":{"conversation_type":3}}'
    assert parse_conversation_from_stream(text) == ("12345", 3)


def test_parse_submit_response_includes_assistant_messages() -> None:
    payload = build_dola_payload(base_payload("verify_test"), "Sidra dancing", 15, "9:16")
    response_text = "\n".join(
        [
            'data: {"message":{"content":"[{\\"content\\":{\\"text_block\\":{\\"text\\":\\"I will create a vertical video of Sidra dancing for you.\\"}}}]"},"conversation_id":"12345","conversation_info":{"conversation_type":3}}',
            'data: {"message":{"content":"[{\\"content\\":{\\"text_block\\":{\\"text\\":\\"The video will be generated using the **Dreamina Seedance 2.0 model**. It will use 3 points and be ready in 1-3 minute.\\"}}}]"}}',
        ]
    )

    result = parse_submit_response(
        DolaSession(
            url="https://www.dola.com/chat/completion?fp=verify_test&web_platform=web",
            headers={"cookie": "i18next=en; flow_user_country=BD; s_v_web_id=verify_test; ttwid=fresh"},
            payload_template={},
            fp="verify_test",
            has_ttwid=True,
            has_hook_slardar=False,
            has_auth_cookies=False,
        ),
        payload,
        Response(200, text=response_text, request=Request("POST", "https://www.dola.com/chat/completion")),
    )

    assert result.conversation_id == "12345"
    assert result.conversation_type == 3
    assert result.assistant_messages == [
        "I will create a vertical video of Sidra dancing for you.",
        "The video will be generated using the **Dreamina Seedance 2.0 model**. It will use 3 points and be ready in 1-3 minute.",
    ]


def test_parse_assistant_messages_redacts_urls() -> None:
    text = 'data: {"message":{"content":"[{\\"content\\":{\\"text_block\\":{\\"text\\":\\"Download at https://secret.example/signed?token=abc\\"}}}]"}}'

    assert parse_assistant_messages_from_stream(text) == ["Download at [redacted-url]"]


def test_parse_assistant_messages_ignores_prompt_echo() -> None:
    text = 'data: {"message":{"content":"[{\\"content\\":{\\"text_block\\":{\\"text\\":\\"Generate exactly 10 seconds video. a man swimming\\"}}}]"}}'

    assert parse_assistant_messages_from_stream(text) == []


def test_extract_chain_texts_from_nested_message() -> None:
    payload = {
        "code": 0,
        "data": {
            "messages": [
                {
                    "content": json.dumps(
                        [
                            {
                                "content": {
                                    "text_block": {
                                        "text": "Processing video request (attempt 1/250)...",
                                    }
                                }
                            }
                        ]
                    )
                }
            ]
        },
    }

    assert extract_chain_texts(payload) == ["Processing video request (attempt 1/250)..."]


def test_parse_conversation_errors() -> None:
    with pytest.raises(ValueError, match="no points"):
        parse_conversation_from_stream('{"error_msg":"no points"}')


def test_parse_vid() -> None:
    assert parse_vid({"code": 0, "data": {"nested": {"vid": "abc_123"}}}) == "abc_123"


def test_parse_vid_from_nested_message_content_string() -> None:
    payload = {
        "code": 0,
        "data": {
            "pull_singe_chain_uplink_body": {
                "messages": [
                    {
                        "content": json.dumps(
                            {
                                "cards": [
                                    {
                                        "video": {
                                            "vid": "nested_vid_123",
                                        }
                                    }
                                ]
                            }
                        )
                    }
                ]
            }
        },
    }

    assert parse_vid(payload) == "nested_vid_123"


def test_parse_vid_reports_checked_paths_when_missing() -> None:
    vid, checked_paths = parse_vid_with_diagnostics({"code": 0, "data": {"messages": []}})

    assert vid is None
    assert "full JSON string" in checked_paths
    assert "data.pull_singe_chain_uplink_body.messages" in checked_paths
    assert "downlink_body.pull_singe_chain_downlink_body.messages" in checked_paths
    assert "recursive JSON/string scan" in checked_paths


def test_policy_rejection_message_is_terminal_failure() -> None:
    message = "The video can't be generated because your input contains content that may violate our policies. Modify it and try again."

    assert is_terminal_video_failure(message) is True


def test_cannot_generate_requested_content_is_terminal_failure() -> None:
    message = "I can't generate the content you requested. Try something else."

    assert is_terminal_video_failure(message) is True


def test_curly_apostrophe_rejection_is_terminal_failure() -> None:
    message = "I can’t generate the content you requested. Try something else."

    assert is_terminal_video_failure(message) is True


def test_raw_response_chunking_preserves_original_body() -> None:
    body = "abc123" * 2000

    assert "".join(split_response_body(body, 101)) == body


def test_parse_play_info() -> None:
    payload = {"code": 0, "data": {"play_infos": [{"main": "https://cdn.example/video.mp4"}]}}
    assert parse_play_info(payload) == "https://cdn.example/video.mp4"


def test_api_logs_route_smoke() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/video/logs")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

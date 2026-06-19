import json

import pytest

from app.services.dola import base_payload, build_dola_payload, parse_conversation_from_stream, parse_play_info, parse_vid


def test_build_payload_includes_seedance_duration_and_prompt() -> None:
    payload = build_dola_payload(base_payload(), "cinematic city flythrough", 15, "9:16")
    assert payload["messages"][0]["content_block"][0]["content"]["text_block"]["text"] == "Generate video: cinematic city flythrough, 9:16"
    assert payload["chat_ability"]["ability_type"] == 17
    assert json.loads(payload["chat_ability"]["ability_param"]) == {"model": "seedance_v2.0", "duration": 15}


def test_parse_conversation_from_stream() -> None:
    text = 'data: {"conversation_id":"12345","conversation_info":{"conversation_type":3}}'
    assert parse_conversation_from_stream(text) == ("12345", 3)


def test_parse_conversation_errors() -> None:
    with pytest.raises(ValueError, match="no points"):
        parse_conversation_from_stream('{"error_msg":"no points"}')


def test_parse_vid() -> None:
    assert parse_vid({"code": 0, "data": {"nested": {"vid": "abc_123"}}}) == "abc_123"


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

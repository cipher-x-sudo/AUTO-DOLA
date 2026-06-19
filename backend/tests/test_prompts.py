import pytest

from app.services.prompts import build_gemini_auth_headers, extract_gemini_text, generate_seedance_prompts, normalize_gemini_base_url, parse_prompt_response


def test_parse_prompt_response_from_json_object() -> None:
    text = '{"prompts":["cinematic car shot","macro product reveal"]}'

    assert parse_prompt_response(text) == ["cinematic car shot", "macro product reveal"]


def test_parse_prompt_response_from_fenced_json() -> None:
    text = '```json\n{"prompts":[{"prompt":"hero tracking shot"}]}\n```'

    assert parse_prompt_response(text) == ["hero tracking shot"]


def test_extract_gemini_text() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": '{"prompts":["one"]}'}, {"text": ""}]}}]}

    assert extract_gemini_text(payload) == '{"prompts":["one"]}'


def test_normalize_gemini_base_url_adds_http_for_localhost() -> None:
    assert normalize_gemini_base_url("localhost:8045") == "http://host.docker.internal:8045/v1beta"


def test_normalize_gemini_base_url_rewrites_localhost_url_for_docker() -> None:
    assert normalize_gemini_base_url("http://127.0.0.1:8045/v1beta") == "http://host.docker.internal:8045/v1beta"


def test_normalize_gemini_base_url_keeps_https_host() -> None:
    assert normalize_gemini_base_url("https://generativelanguage.googleapis.com/v1beta/") == "https://generativelanguage.googleapis.com/v1beta"


def test_build_gemini_auth_headers_supports_local_proxy_auth_styles() -> None:
    headers = build_gemini_auth_headers(" secret-token ")

    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["x-goog-api-key"] == "secret-token"
    assert headers["x-api-key"] == "secret-token"


@pytest.mark.asyncio
async def test_generate_prompts_requires_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        await generate_seedance_prompts("car", 2, 15, "9:16", "cinematic", "", "https://example.test", "gemini")

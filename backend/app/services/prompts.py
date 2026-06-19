from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


def build_prompt_system_instruction(count: int, duration: int, ratio: str, style: str) -> str:
    return (
        "You are an expert Seedance 2.0 video prompt engineer. "
        f"Create exactly {count} distinct text-to-video prompts for {duration} second videos in {ratio} format. "
        f"Use the style direction: {style}. "
        "Each prompt must be one complete line, vivid, cinematic, and ready to paste into a video generator. "
        "Include subject, action, setting, camera movement, lighting, motion, realism, and quality details. "
        "Avoid numbering inside the prompt text. Avoid policy-sensitive or violent instructions. "
        "Return only valid JSON shaped like {\"prompts\":[\"...\"]}."
    )


async def generate_seedance_prompts(
    master_prompt: str,
    count: int,
    duration: int,
    ratio: str,
    style: str,
    api_key: str,
    base_url: str,
    model: str,
) -> list[str]:
    if not api_key.strip():
        raise ValueError("Gemini API key is missing. Add it in Prompt Generator settings.")
    clean_base_url = normalize_gemini_base_url(base_url)
    url = f"{clean_base_url}/models/{model}:generateContent"
    headers = build_gemini_auth_headers(api_key)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            f"Master idea: {master_prompt}\n"
                            f"Generate {count} Seedance prompts. Duration: {duration}. Aspect ratio: {ratio}. Style: {style}."
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.8,
            "responseMimeType": "application/json",
        },
        "systemInstruction": {"parts": [{"text": build_prompt_system_instruction(count, duration, ratio, style)}]},
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(url, params={"key": api_key}, headers=headers, json=payload)
    if response.status_code >= 400:
        detail = response.text[:500] or "No response body. Check Gemini API key/token and model/host path."
        raise RuntimeError(f"Gemini API returned HTTP {response.status_code}: {detail}")
    text = extract_gemini_text(response.json())
    prompts = parse_prompt_response(text)
    if not prompts:
        raise RuntimeError("Gemini returned no prompts.")
    return prompts[:count]


def normalize_gemini_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/") or "https://generativelanguage.googleapis.com/v1beta"
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        value = f"http://{value}"
    parsed = urlsplit(value)
    is_local_host = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if is_local_host:
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"host.docker.internal{port}"
        value = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        parsed = urlsplit(value)
    if parsed.hostname == "host.docker.internal" and parsed.path in {"", "/"}:
        value = urlunsplit((parsed.scheme, parsed.netloc, "/v1beta", parsed.query, parsed.fragment))
    return value.rstrip("/")


def build_gemini_auth_headers(api_key: str) -> dict[str, str]:
    token = api_key.strip()
    return {
        "Authorization": f"Bearer {token}",
        "x-goog-api-key": token,
        "x-api-key": token,
    }


def extract_gemini_text(payload: dict[str, Any]) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "\n".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()


def parse_prompt_response(text: str) -> list[str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return [line.strip(" -0123456789.").strip() for line in cleaned.splitlines() if line.strip()]
    if isinstance(parsed, dict):
        values = parsed.get("prompts", [])
    elif isinstance(parsed, list):
        values = parsed
    else:
        values = []
    prompts: list[str] = []
    for value in values:
        if isinstance(value, str):
            prompt = value
        elif isinstance(value, dict):
            prompt = str(value.get("prompt", ""))
        else:
            prompt = str(value)
        prompt = re.sub(r"\s+", " ", prompt).strip()
        if prompt:
            prompts.append(prompt)
    return prompts

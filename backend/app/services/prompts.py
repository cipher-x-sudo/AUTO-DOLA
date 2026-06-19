from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

PROMPT_BATCH_SIZE = 5


def build_prompt_system_instruction(count: int, duration: int, ratio: str, style: str, batch_number: int = 1) -> str:
    return (
        "You are an expert Seedance 2.0 video prompt engineer. "
        f"Create exactly {count} distinct text-to-video prompts for batch {batch_number}. "
        f"Each prompt is for a {duration} second video in {ratio} format. "
        f"Use the style direction: {style}. "
        "Each prompt must be one complete line, vivid, cinematic, and ready to paste into a video generator. "
        "Include a specific subject, action, setting, camera movement, lighting, motion, realism, and quality details. "
        "Make every prompt meaningfully different in action, environment, camera language, and emotional hook. "
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
    prompts: list[str] = []
    seen: set[str] = set()
    batch_number = 1
    async with httpx.AsyncClient(timeout=90) as client:
        while len(prompts) < count:
            remaining = count - len(prompts)
            batch_count = min(PROMPT_BATCH_SIZE, remaining)
            batch = await request_prompt_batch(
                client,
                url,
                headers,
                api_key,
                master_prompt,
                batch_count,
                duration,
                ratio,
                style,
                prompts,
                batch_number,
            )
            for prompt in batch:
                key = normalize_prompt_key(prompt)
                if key and key not in seen:
                    seen.add(key)
                    prompts.append(prompt)
                if len(prompts) >= count:
                    break
            if not batch:
                raise RuntimeError(f"Gemini returned no prompts for batch {batch_number}.")
            batch_number += 1
            if batch_number > count + 10:
                break
    if not prompts:
        raise RuntimeError("Gemini returned no prompts.")
    if len(prompts) < count:
        raise RuntimeError(f"Gemini returned only {len(prompts)} unique prompts out of {count} requested.")
    return prompts[:count]


async def request_prompt_batch(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    api_key: str,
    master_prompt: str,
    count: int,
    duration: int,
    ratio: str,
    style: str,
    existing_prompts: list[str],
    batch_number: int,
) -> list[str]:
    avoid_text = "\n".join(f"- {prompt}" for prompt in existing_prompts[-30:])
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            f"Master idea: {master_prompt}\n"
                            f"Generate this batch of {count} Seedance prompts. Duration: {duration}. Aspect ratio: {ratio}. Style: {style}.\n"
                            "Do not repeat any previous wording, action, setting, camera movement, or scene concept.\n"
                            f"Previous prompts to avoid:\n{avoid_text if avoid_text else '- none yet'}"
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.8,
            "responseMimeType": "application/json",
        },
        "systemInstruction": {"parts": [{"text": build_prompt_system_instruction(count, duration, ratio, style, batch_number)}]},
    }
    response = await client.post(url, params={"key": api_key}, headers=headers, json=payload)
    if response.status_code >= 400:
        detail = response.text[:500] or "No response body. Check Gemini API key/token and model/host path."
        raise RuntimeError(f"Gemini API returned HTTP {response.status_code}: {detail}")
    text = extract_gemini_text(response.json())
    return parse_prompt_response(text)[:count]


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


def normalize_prompt_key(prompt: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", prompt.lower()).strip()

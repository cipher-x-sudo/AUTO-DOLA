from __future__ import annotations

from pathlib import Path

import httpx


async def generate_image(prompt: str, aspect_ratio: str, api_key: str, output_path: Path) -> Path:
    if not api_key:
        raise ValueError("YousMind API key is required for image generation.")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post("https://yousmind.com/api/v1/images/generations", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"prompt": prompt, "aspect_ratio": aspect_ratio})
        response.raise_for_status()
        payload = response.json()
        image_url = payload.get("url") or payload.get("data", [{}])[0].get("url")
        if not image_url:
            raise ValueError("Image API did not return an image URL.")
        image = await client.get(image_url)
        image.raise_for_status()
    output_path.write_bytes(image.content)
    return output_path

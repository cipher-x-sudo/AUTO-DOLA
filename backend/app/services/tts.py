from __future__ import annotations

from pathlib import Path

import edge_tts


async def list_voices() -> list[dict]:
    return await edge_tts.list_voices()


async def synthesize(text: str, voice: str, output_path: Path) -> Path:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))
    return output_path

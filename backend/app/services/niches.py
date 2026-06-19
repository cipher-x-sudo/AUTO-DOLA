from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.services.prompts import generate_seedance_prompts


@dataclass(frozen=True)
class NicheFile:
    id: str
    name: str
    filename: str
    size_bytes: int
    path: Path


def safe_stem(value: str, fallback: str = "niche") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()[:90].strip("-")
    return cleaned or fallback


def display_name_from_stem(stem: str) -> str:
    return re.sub(r"\s+", " ", stem.replace("-", " ")).strip().title()


def list_niches(niches_dir: Path | None = None) -> list[NicheFile]:
    root = (niches_dir or settings.niches_dir).resolve()
    if not root.exists():
        return []
    niches: list[NicheFile] = []
    for path in sorted(root.glob("*.txt"), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        stem = path.stem
        niches.append(NicheFile(id=stem, name=display_name_from_stem(stem), filename=path.name, size_bytes=path.stat().st_size, path=path.resolve()))
    return niches


def resolve_niches(niche_ids: list[str], niches_dir: Path | None = None) -> list[NicheFile]:
    by_id = {niche.id: niche for niche in list_niches(niches_dir)}
    missing = [niche_id for niche_id in niche_ids if niche_id not in by_id]
    if missing:
        raise ValueError(f"Unknown niche id(s): {', '.join(missing[:5])}")
    return [by_id[niche_id] for niche_id in niche_ids]


def split_global_count(total: int, niche_count: int) -> list[int]:
    if total < 1:
        raise ValueError("Prompt count must be at least 1.")
    if niche_count < 1:
        raise ValueError("Select at least one niche.")
    if total < niche_count:
        raise ValueError("Global prompt count must be at least the selected niche count.")
    counts = [total // niche_count for _ in range(niche_count)]
    remainder = total % niche_count
    indexes = list(range(niche_count))
    random.shuffle(indexes)
    for index in indexes[:remainder]:
        counts[index] += 1
    return counts


def versioned_path(directory: Path, stem: str, suffix: str = ".txt") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def save_generated_prompts(niche: NicheFile, prompts: list[str], output_dir: Path | None = None) -> Path:
    stem = safe_stem(f"{niche.id}-prompts", "niche-prompts")
    path = versioned_path((output_dir or settings.generated_prompts_dir).resolve(), stem)
    content = "\n".join(f"{index + 1}. {prompt}" for index, prompt in enumerate(prompts))
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")
    return path


async def generate_for_niches(
    niche_ids: list[str],
    count: int,
    count_mode: str,
    duration: int,
    style: str,
    api_key: str,
    base_url: str,
    model: str,
    *,
    niches_dir: Path | None = None,
    output_dir: Path | None = None,
    existing_prompts: list[str] | None = None,
    save: bool = True,
) -> list[dict]:
    niches = resolve_niches(niche_ids, niches_dir)
    if count_mode not in {"global", "per_niche"}:
        raise ValueError("count_mode must be global or per_niche.")
    counts = split_global_count(count, len(niches)) if count_mode == "global" else [count for _ in niches]
    groups: list[dict] = []
    for niche, prompt_count in zip(niches, counts, strict=True):
        if prompt_count <= 0:
            continue
        master_prompt = niche.path.read_text(encoding="utf-8")
        if existing_prompts:
            avoid = "\n".join(f"{index + 1}. {prompt}" for index, prompt in enumerate(existing_prompts[-80:]))
            master_prompt = f"{master_prompt}\n\nAlready generated prompts. Do not repeat concepts, wording, camera moves, subjects, or settings:\n{avoid}"
        prompts = await generate_seedance_prompts(master_prompt, prompt_count, duration, "9:16", style, api_key, base_url, model)
        saved_path = save_generated_prompts(niche, prompts, output_dir) if save else None
        groups.append(
            {
                "niche_id": niche.id,
                "niche_name": niche.name,
                "filename": niche.filename,
                "requested_count": prompt_count,
                "prompts": prompts,
                "saved_path": str(saved_path) if saved_path else "",
            }
        )
    return groups


def save_niche_prompt_group(niche_id: str, prompts: list[str], *, niches_dir: Path | None = None, output_dir: Path | None = None) -> Path:
    niche = resolve_niches([niche_id], niches_dir)[0]
    return save_generated_prompts(niche, prompts, output_dir)

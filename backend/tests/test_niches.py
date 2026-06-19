from __future__ import annotations

from pathlib import Path

import pytest

from app.services import niches as niche_service


def write_niche(root: Path, name: str, body: str = "Make strong viral Seedance prompts.") -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


def test_list_niches_returns_only_txt_files(tmp_path: Path) -> None:
    write_niche(tmp_path, "giant-creature.txt")
    (tmp_path / "ignore.md").write_text("nope", encoding="utf-8")

    niches = niche_service.list_niches(tmp_path)

    assert [niche.id for niche in niches] == ["giant-creature"]
    assert niches[0].name == "Giant Creature"
    assert niches[0].filename == "giant-creature.txt"


def test_resolve_niches_rejects_unknown_ids(tmp_path: Path) -> None:
    write_niche(tmp_path, "known.txt")

    with pytest.raises(ValueError, match="Unknown niche"):
        niche_service.resolve_niches(["missing"], tmp_path)


def test_split_global_count_totals_and_covers_every_selected_niche() -> None:
    counts = niche_service.split_global_count(11, 5)

    assert sum(counts) == 11
    assert len(counts) == 5
    assert all(count >= 2 for count in counts)


def test_split_global_count_rejects_less_than_selected_niches() -> None:
    with pytest.raises(ValueError, match="at least the selected niche count"):
        niche_service.split_global_count(2, 3)


def test_save_generated_prompts_uses_versioned_safe_names(tmp_path: Path) -> None:
    niche = niche_service.NicheFile(id="giant/creature", name="Giant Creature", filename="giant-creature.txt", size_bytes=10, path=tmp_path / "x.txt")

    first = niche_service.save_generated_prompts(niche, ["one"], tmp_path)
    second = niche_service.save_generated_prompts(niche, ["two"], tmp_path)

    assert first.name == "giant-creature-prompts.txt"
    assert second.name == "giant-creature-prompts-2.txt"
    assert first.read_text(encoding="utf-8") == "1. one\n"
    assert second.read_text(encoding="utf-8") == "1. two\n"


@pytest.mark.asyncio
async def test_generate_for_niches_per_niche_uses_requested_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_niche(tmp_path, "alpha.txt", "alpha context")
    write_niche(tmp_path, "beta.txt", "beta context")
    output_dir = tmp_path / "generated"
    calls: list[tuple[str, int]] = []

    async def fake_generate(master_prompt: str, count: int, *_args: object) -> list[str]:
        calls.append((master_prompt, count))
        return [f"{master_prompt} prompt {index + 1}" for index in range(count)]

    monkeypatch.setattr(niche_service, "generate_seedance_prompts", fake_generate)

    groups = await niche_service.generate_for_niches(["alpha", "beta"], 3, "per_niche", 15, "cinematic realistic", "key", "host", "model", niches_dir=tmp_path, output_dir=output_dir)

    assert [call[1] for call in calls] == [3, 3]
    assert [len(group["prompts"]) for group in groups] == [3, 3]
    assert all(Path(group["saved_path"]).exists() for group in groups)


@pytest.mark.asyncio
async def test_generate_for_niches_global_splits_total(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("alpha.txt", "beta.txt", "gamma.txt"):
        write_niche(tmp_path, name, name)
    output_dir = tmp_path / "generated"
    counts: list[int] = []

    async def fake_generate(_master_prompt: str, count: int, *_args: object) -> list[str]:
        counts.append(count)
        return [f"prompt {index + 1}" for index in range(count)]

    monkeypatch.setattr(niche_service, "generate_seedance_prompts", fake_generate)

    groups = await niche_service.generate_for_niches(["alpha", "beta", "gamma"], 8, "global", 15, "cinematic realistic", "key", "host", "model", niches_dir=tmp_path, output_dir=output_dir)

    assert sum(counts) == 8
    assert len(groups) == 3
    assert sum(len(group["prompts"]) for group in groups) == 8

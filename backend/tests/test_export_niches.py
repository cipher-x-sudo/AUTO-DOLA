from pathlib import Path

import httpx
import pytest

from backend.scripts.export_niches import (
    export_niches,
    extract_google_doc_id,
    parse_sheet_rows,
    sanitize_filename,
    versioned_path,
)


def test_sanitize_filename() -> None:
    assert sanitize_filename("Giant Creature Encounter", "fallback") == "giant-creature-encounter"
    assert sanitize_filename("bad/name: niche!", "fallback") == "bad-name-niche"
    assert sanitize_filename("!!!", "fallback") == "fallback"


def test_extract_google_doc_id() -> None:
    assert extract_google_doc_id("https://docs.google.com/document/d/abc123/edit?usp=sharing") == "abc123"
    assert extract_google_doc_id("https://example.com/nope") is None


def test_versioned_path(tmp_path: Path) -> None:
    (tmp_path / "niche.txt").write_text("one", encoding="utf-8")
    (tmp_path / "niche-2.txt").write_text("two", encoding="utf-8")

    assert versioned_path(tmp_path, "niche").name == "niche-3.txt"


def test_parse_sheet_rows() -> None:
    csv_text = "PROMPT NAME ,SHORT/LONG,LINK\nCreature,short,https://docs.google.com/document/d/doc1/edit\n"

    assert parse_sheet_rows(csv_text) == [{"name": "Creature", "link": "https://docs.google.com/document/d/doc1/edit"}]


def test_parse_sheet_rows_requires_columns() -> None:
    with pytest.raises(ValueError, match="PROMPT NAME and LINK"):
        parse_sheet_rows("Name,Url\nx,y\n")


def test_export_niches_with_mocked_google(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sheet = (
        "PROMPT NAME ,SHORT/LONG,LINK\n"
        "Creature,short,https://docs.google.com/document/d/doc1/edit\n"
        "Creature,short,https://docs.google.com/document/d/doc2/edit\n"
        "Missing Link,short,\n"
        "Bad Link,short,https://example.com/nope\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "spreadsheets" in url:
            return httpx.Response(200, text=sheet)
        if "/doc1/" in url:
            return httpx.Response(200, text="Doc one text")
        if "/doc2/" in url:
            return httpx.Response(200, text="Doc two text")
        return httpx.Response(404)

    original_client = httpx.Client

    def mock_client(*args: object, **kwargs: object) -> httpx.Client:
        return original_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(httpx, "Client", mock_client)

    result = export_niches("https://docs.google.com/spreadsheets/d/sheet/export?format=csv", tmp_path)

    assert result.total_rows == 4
    assert result.saved == 2
    assert result.skipped == 2
    assert result.failed == 0
    assert (tmp_path / "creature.txt").read_text(encoding="utf-8") == "Doc one text"
    assert (tmp_path / "creature-2.txt").read_text(encoding="utf-8") == "Doc two text"

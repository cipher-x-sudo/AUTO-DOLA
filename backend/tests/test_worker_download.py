from pathlib import Path

import pytest

import app.worker as worker


class FakeStream:
    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self.chunks = chunks
        self.status_code = status_code
        self.headers = {"content-type": "video/mp4", "content-length": str(sum(len(chunk) for chunk in chunks))}

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class FakeAsyncClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, _method: str, _url: str) -> FakeStream:
        return FakeStream(self.chunks)


@pytest.mark.asyncio
async def test_download_file_writes_part_then_renames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient([b"abc", b"123"]))
    target = tmp_path / "video_raw.mp4"

    info = await worker.download_file("https://cdn.example.com/video.mp4", target)

    assert target.read_bytes() == b"abc123"
    assert not (tmp_path / "video_raw.mp4.part").exists()
    assert info["bytes_written"] == 6
    assert info["url_host"] == "cdn.example.com"
    assert info["raw_exists_after_download"] is True


@pytest.mark.asyncio
async def test_download_file_rejects_empty_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient([]))

    with pytest.raises(worker.DownloadError) as exc_info:
        await worker.download_file("https://cdn.example.com/video.mp4", tmp_path / "video_raw.mp4")

    assert exc_info.value.code == "DOWNLOAD_EMPTY"
    assert not (tmp_path / "video_raw.mp4").exists()
    assert not (tmp_path / "video_raw.mp4.part").exists()


def test_save_downloaded_video_fails_before_cleanup_when_raw_missing(tmp_path: Path) -> None:
    with pytest.raises(worker.DownloadError) as exc_info:
        worker.save_downloaded_video(
            tmp_path / "missing_raw.mp4",
            tmp_path / "final.mp4",
            "final",
            True,
            lambda *_args: None,
            lambda *_args: None,
        )

    assert exc_info.value.code == "DOWNLOAD_FILE_MISSING"


def test_save_downloaded_video_uses_final_if_cleanup_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = tmp_path / "video_raw.mp4"
    final = tmp_path / "video.mp4"
    raw.write_bytes(b"raw")

    def fake_clean(input_path: Path, output_path: Path) -> bool:
        output_path.write_bytes(b"final")
        return True

    monkeypatch.setattr(worker, "clean_video", fake_clean)

    artifact = worker.save_downloaded_video(raw, final, "final", True, lambda *_args: None, lambda *_args: None)

    assert artifact == final
    assert final.read_bytes() == b"final"
    assert not raw.exists()

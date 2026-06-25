import pytest

import app.services.dola_browser as dola_browser

from app.services.dola_browser import (
    BrowserNetworkState,
    DolaBrowserClient,
    DolaBrowserSubmitResult,
    build_browser_video_prompt_text,
    conversation_id_from_url,
    fp_from_url_or_cookies,
    has_auth_cookie,
    playwright_proxy_config,
    proxy_public_host,
    sanitize_replay_headers,
    vid_from_download_url,
)
from app.services.dola import DolaSession


def test_sanitize_replay_headers_removes_transport_and_cookie_headers() -> None:
    headers = {
        "host": "www.dola.com",
        "content-length": "123",
        "accept-encoding": "gzip",
        "cookie": "ttwid=secret",
        "user-agent": "Chrome",
        "content-type": "application/json",
    }

    assert sanitize_replay_headers(headers) == {
        "user-agent": "Chrome",
        "content-type": "application/json",
    }


def test_conversation_id_from_url() -> None:
    assert conversation_id_from_url("https://www.dola.com/chat/38415185039130385") == "38415185039130385"


def test_conversation_id_from_url_returns_none() -> None:
    assert conversation_id_from_url("https://www.dola.com/chat/") is None


def test_fp_from_url_prefers_query() -> None:
    assert fp_from_url_or_cookies("https://www.dola.com/chat/completion?fp=verify_123", {"s_v_web_id": "cookie_fp"}) == "verify_123"


def test_fp_from_url_falls_back_to_cookie() -> None:
    assert fp_from_url_or_cookies("https://www.dola.com/chat/completion", {"s_v_web_id": "cookie_fp"}) == "cookie_fp"


def test_has_auth_cookie_detects_auth_like_names() -> None:
    assert has_auth_cookie({"sid_guard": "x"}) is True
    assert has_auth_cookie({"ttwid": "x"}) is False


def test_playwright_proxy_config_supports_auth_url() -> None:
    assert playwright_proxy_config("http://user:pa%40ss@proxy.example.com:2312") == {
        "server": "http://proxy.example.com:2312",
        "username": "user",
        "password": "pa@ss",
    }


def test_proxy_public_host_redacts_credentials() -> None:
    assert proxy_public_host("http://user:pass@proxy.example.com:2312") == "proxy.example.com:2312"


def test_vid_from_download_url() -> None:
    url = "https://v16-dola.dola.com/x/video/tos/mya/tos-mya-ve-50851/okqOYb95GBfIYxFNgF7E0Q9QBPqhwrfMNUhDEw/?download=true"

    assert vid_from_download_url(url) == "okqOYb95GBfIYxFNgF7E0Q9QBPqhwrfMNUhDEw"


def test_build_browser_video_prompt_text_uses_selected_duration() -> None:
    assert build_browser_video_prompt_text("cinematic city", 15, "9:16") == "Generate exactly 15 seconds vertical 9:16 video. cinematic city"
    assert build_browser_video_prompt_text("cinematic city", 10, "9:16") == "Generate exactly 10 seconds vertical 9:16 video. cinematic city"
    assert build_browser_video_prompt_text("cinematic city", 5, "9:16") == "Generate exactly 5 seconds vertical 9:16 video. cinematic city"


@pytest.mark.asyncio
async def test_wait_for_submit_capture_requires_conversation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dola_browser, "BROWSER_SUBMIT_CAPTURE_TIMEOUT_SECONDS", 0)
    client = DolaBrowserClient()
    network = BrowserNetworkState(captured_url="https://www.dola.com/chat/completion")

    class FakePage:
        url = "https://www.dola.com/chat/"

    with pytest.raises(Exception, match="conversation_id was not captured"):
        await client._wait_for_submit_capture(None, FakePage(), network)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_new_job_page_allocates_separate_pages() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = ""

        async def goto(self, url: str, **_: object) -> None:
            self.url = url

    class FakeContext:
        def __init__(self) -> None:
            self.pages: list[FakePage] = []

        async def new_page(self) -> FakePage:
            page = FakePage()
            self.pages.append(page)
            return page

    context = FakeContext()
    client = DolaBrowserClient()

    first = await client.new_job_page(context)  # type: ignore[arg-type]
    second = await client.new_job_page(context)  # type: ignore[arg-type]

    assert first is not second
    assert len(context.pages) == 2
    assert first.url == "https://www.dola.com/chat/create-image"
    assert second.url == "https://www.dola.com/chat/create-image"


@pytest.mark.asyncio
async def test_submit_browser_flow_navigates_selects_video_and_submits(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    submitted_prompts: list[str] = []

    class FakePage:
        def __init__(self) -> None:
            self.url = ""

        def on(self, *_args: object) -> None:
            pass

        async def goto(self, url: str, **_: object) -> None:
            events.append("goto")
            self.url = url

    class FakeContext:
        def __init__(self) -> None:
            self.pages: list[FakePage] = []
            self.scripts: list[str] = []

        async def add_init_script(self, script: str) -> None:
            events.append("hook")
            self.scripts.append(script)

        async def new_page(self) -> FakePage:
            assert self.scripts == []
            page = FakePage()
            self.pages.append(page)
            return page

    client = DolaBrowserClient()
    context = FakeContext()

    async def fake_launch_slot() -> dict[str, object]:
        return {"slot_id": "slot-1", "slot_number": 1}

    async def fake_connect_slot(_slot: dict[str, object]) -> dict[str, object]:
        return {"context": context, "playwright": object(), "browser": object(), "slot": _slot}

    async def fake_noop(*_args: object, **_kwargs: object) -> None:
        pass

    async def fake_select_video(_page: FakePage) -> None:
        events.append("select-video")

    async def fake_submit(_page: FakePage, prompt: str) -> None:
        events.append("submit")
        submitted_prompts.append(prompt)

    async def fake_wait(_context: FakeContext, _page: FakePage, _network: BrowserNetworkState) -> DolaBrowserSubmitResult:
        return DolaBrowserSubmitResult(
            session=DolaSession(
                url="https://www.dola.com/chat/completion?fp=verify_test&web_platform=web",
                headers={},
                payload_template={},
                fp="verify_test",
                has_ttwid=True,
                has_hook_slardar=False,
                has_auth_cookies=False,
            ),
            conversation_id="123456789",
            conversation_type=3,
            diagnostic={},
        )

    monkeypatch.setattr(client, "_launch_slot", fake_launch_slot)
    monkeypatch.setattr(client, "_connect_slot", fake_connect_slot)
    monkeypatch.setattr(client, "_ensure_dola_ready", fake_noop)
    monkeypatch.setattr(client, "_raise_if_blocked", fake_noop)
    monkeypatch.setattr(client, "_select_video_mode", fake_select_video)
    monkeypatch.setattr(client, "_submit_via_ui", fake_submit)
    monkeypatch.setattr(client, "_wait_for_submit_capture", fake_wait)

    result = await client.submit_and_capture_session("cinematic city", 15, "9:16")

    assert result.slot_id == "slot-1"
    assert events == ["goto", "select-video", "submit"]
    assert context.pages[0].url == "https://www.dola.com/chat/create-image"
    assert submitted_prompts == ["Generate exactly 15 seconds vertical 9:16 video. cinematic city"]

import pytest

import app.services.dola_browser as dola_browser

from app.services.dola_browser import (
    BrowserNetworkState,
    DURATION_PATCH_SCRIPT,
    DolaBrowserError,
    DolaBrowserClient,
    DolaBrowserSubmitResult,
    build_browser_video_prompt_text,
    extract_duration_and_ratio_from_post_data,
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


@pytest.mark.asyncio
async def test_proxy_auth_is_handled_through_cdp() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.handlers: dict[str, object] = {}
            self.commands: list[tuple[str, dict[str, object]]] = []

        def on(self, event: str, callback: object) -> None:
            self.handlers[event] = callback

        async def send(self, method: str, params: dict[str, object]) -> None:
            self.commands.append((method, params))

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [object()]
            self.session = FakeSession()
            self.handlers: dict[str, object] = {}

        async def new_cdp_session(self, _page: object) -> FakeSession:
            return self.session

        def on(self, event: str, callback: object) -> None:
            self.handlers[event] = callback

    client = DolaBrowserClient()
    context = FakeContext()
    runtime: dict[str, object] = {}

    await client._install_proxy_auth_handlers(
        context,  # type: ignore[arg-type]
        {"proxy_username": "user", "proxy_password": "secret", "proxy_auth_mode": "cdp"},
        runtime,
    )

    assert ("Fetch.enable", {"handleAuthRequests": True}) in context.session.commands
    callback = context.session.handlers["Fetch.authRequired"]
    task = callback({"requestId": "request-1"})  # type: ignore[operator]
    await task
    assert (
        "Fetch.continueWithAuth",
        {
            "requestId": "request-1",
            "authChallengeResponse": {
                "response": "ProvideCredentials",
                "username": "user",
                "password": "secret",
            },
        },
    ) in context.session.commands
    assert runtime["proxy_auth_mode"] == "cdp"


def test_vid_from_download_url() -> None:
    url = "https://v16-dola.dola.com/x/video/tos/mya/tos-mya-ve-50851/okqOYb95GBfIYxFNgF7E0Q9QBPqhwrfMNUhDEw/?download=true"

    assert vid_from_download_url(url) == "okqOYb95GBfIYxFNgF7E0Q9QBPqhwrfMNUhDEw"


def test_build_browser_video_prompt_text_uses_visible_ten_for_fifteen_second_patch() -> None:
    assert build_browser_video_prompt_text("cinematic 15-second city", 15, "9:16") == "Generate exactly 10 seconds vertical 9:16 video. cinematic 10-second city"
    assert build_browser_video_prompt_text("cinematic city", 10, "9:16") == "Generate exactly 10 seconds vertical 9:16 video. cinematic city"
    assert build_browser_video_prompt_text("cinematic city", 5, "9:16") == "Generate exactly 5 seconds vertical 9:16 video. cinematic city"


def test_duration_patch_script_rewrites_five_or_ten_to_fifteen() -> None:
    assert "JSON.stringify" in DURATION_PATCH_SCRIPT
    assert '"duration":15' in DURATION_PATCH_SCRIPT
    assert "(10|5)" in DURATION_PATCH_SCRIPT


def test_extract_duration_and_ratio_from_post_data() -> None:
    assert extract_duration_and_ratio_from_post_data('{"chat_ability":{"ability_param":"{\\"duration\\":15,\\"ratio\\":\\"9:16\\"}"}}') == (15, "9:16")


@pytest.mark.asyncio
async def test_close_slot_returns_manager_cleanup_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePlaywright:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, bool]:
            return {"closed": True}

    class FakeAsyncClient:
        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def post(self, url: str, json: dict[str, str]) -> FakeResponse:
            assert url.endswith("/close")
            assert json == {"slot_id": "slot-1"}
            return FakeResponse()

    monkeypatch.setattr(dola_browser.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient())
    client = DolaBrowserClient(manager_url="http://browser-manager:7070")
    fake_playwright = FakePlaywright()
    client._active_slots["slot-1"] = {"playwright": fake_playwright}

    assert await client.close_slot("slot-1") is True
    assert fake_playwright.stopped is True
    assert "slot-1" not in client._active_slots


@pytest.mark.asyncio
async def test_submit_failure_before_page_closes_launched_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DolaBrowserClient()
    closed_slots: list[str] = []
    logs: list[tuple[str, str]] = []

    async def fake_launch_slot() -> dict[str, object]:
        return {"slot_id": "slot-1", "slot_number": 1}

    async def fake_connect_slot(_slot: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("connect failed")

    async def fake_close_slot(slot_id: str) -> bool:
        closed_slots.append(slot_id)
        return True

    monkeypatch.setattr(client, "_launch_slot", fake_launch_slot)
    monkeypatch.setattr(client, "_connect_slot", fake_connect_slot)
    monkeypatch.setattr(client, "close_slot", fake_close_slot)

    with pytest.raises(DolaBrowserError, match="connect failed"):
        await client.submit_and_capture_session("prompt", 15, "9:16", log_fn=lambda message, level: logs.append((message, level)))

    assert closed_slots == ["slot-1"]
    assert ("Deleting browser profile after rejection", "info") in logs
    assert ("Browser profile cleaned", "success") in logs


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
async def test_new_job_page_reuses_blank_startup_page_and_closes_extra_blank_pages() -> None:
    class FakePage:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False

        async def goto(self, url: str, **_: object) -> None:
            self.url = url

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage("about:blank"), FakePage("about:blank")]

        async def new_page(self) -> FakePage:
            raise AssertionError("blank startup tab should be reused")

    context = FakeContext()
    client = DolaBrowserClient()

    page = await client.new_job_page(context)  # type: ignore[arg-type]

    assert page is context.pages[0]
    assert page.url == "https://www.dola.com/chat/create-image"
    assert context.pages[1].closed is True


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
            assert self.scripts == [DURATION_PATCH_SCRIPT]
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
    assert events == ["hook", "goto", "select-video", "submit"]
    assert context.pages[0].url == "https://www.dola.com/chat/create-image"
    assert submitted_prompts == ["Generate exactly 10 seconds vertical 9:16 video. cinematic city"]


@pytest.mark.asyncio
async def test_wait_for_ready_video_download_clicks_ready_card_and_returns_video_src(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DolaBrowserClient()
    clicked = False
    logs: list[tuple[str, str]] = []

    class FakePage:
        url = "https://www.dola.com/chat/123456789"

        def on(self, *_args: object) -> None:
            pass

        async def wait_for_timeout(self, _timeout: int) -> None:
            pass

    async def fake_slot_page(_slot_id: str, _conversation_id: str) -> FakePage:
        return FakePage()

    async def fake_click_ready(_page: FakePage) -> bool:
        nonlocal clicked
        clicked = True
        return True

    async def fake_video_src(_page: FakePage) -> str:
        return "https://v16-dola.dola.com/x/video/tos/mya/tos-mya-ve-50851/video_abc/?download=true" if clicked else ""

    monkeypatch.setattr(client, "_slot_page", fake_slot_page)
    monkeypatch.setattr(client, "_click_ready_video_card", fake_click_ready)
    monkeypatch.setattr(client, "_video_src_from_dom", fake_video_src)

    result = await client.wait_for_ready_video_download(
        "123456789",
        slot_id="slot-1",
        timeout_seconds=1,
        log_fn=lambda message, level: logs.append((message, level)),
    )

    assert result.download_url.endswith("download=true")
    assert result.vid == "video_abc"
    assert ("Browser says video ready.", "success") in logs
    assert ("Opened ready Dola video card to capture play_info.", "info") in logs

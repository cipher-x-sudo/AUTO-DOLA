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
    format_browser_diagnostic,
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
    assert runtime["images_blocked"] is True

    image_callback = context.session.handlers["Fetch.requestPaused"]
    image_task = image_callback({"requestId": "image-1", "resourceType": "Image"})  # type: ignore[operator]
    await image_task
    assert ("Fetch.failRequest", {"requestId": "image-1", "errorReason": "BlockedByClient"}) in context.session.commands
    assert runtime["resource_stats"]["blocked_image_count"] == 1  # type: ignore[index]

    xhr_task = image_callback({"requestId": "xhr-1", "resourceType": "XHR"})  # type: ignore[operator]
    await xhr_task
    assert ("Fetch.continueRequest", {"requestId": "xhr-1"}) in context.session.commands


@pytest.mark.asyncio
async def test_image_blocking_is_installed_without_proxy_auth() -> None:
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

        async def new_cdp_session(self, _page: object) -> FakeSession:
            return self.session

        def on(self, _event: str, _callback: object) -> None:
            pass

    client = DolaBrowserClient()
    context = FakeContext()
    runtime: dict[str, object] = {}

    await client._install_proxy_auth_handlers(context, {"proxy_auth_mode": "none"}, runtime)  # type: ignore[arg-type]

    assert ("Fetch.enable", {"handleAuthRequests": False}) in context.session.commands
    assert "Fetch.authRequired" not in context.session.handlers
    assert "Fetch.requestPaused" in context.session.handlers


def test_vid_from_download_url() -> None:
    url = "https://v16-dola.dola.com/x/video/tos/mya/tos-mya-ve-50851/okqOYb95GBfIYxFNgF7E0Q9QBPqhwrfMNUhDEw/?download=true"

    assert vid_from_download_url(url) == "okqOYb95GBfIYxFNgF7E0Q9QBPqhwrfMNUhDEw"


def test_build_browser_video_prompt_text_uses_simple_prefix_for_all_settings() -> None:
    expected = "Generate video: cinematic 15-second city"
    for duration in (5, 10, 15):
        for ratio in ("9:16", "16:9", "1:1"):
            assert build_browser_video_prompt_text("cinematic 15-second city", duration, ratio) == expected
    assert build_browser_video_prompt_text("Generate video: cinematic city", 10, "9:16") == "Generate video: cinematic city"


def test_duration_patch_script_rewrites_five_or_ten_to_fifteen() -> None:
    assert "JSON.stringify" in DURATION_PATCH_SCRIPT
    assert '"duration":15' in DURATION_PATCH_SCRIPT
    assert "(10|5)" in DURATION_PATCH_SCRIPT


def test_extract_duration_and_ratio_from_post_data() -> None:
    assert extract_duration_and_ratio_from_post_data('{"chat_ability":{"ability_param":"{\\"duration\\":15,\\"ratio\\":\\"9:16\\"}"}}') == (15, "9:16")


def test_browser_diagnostic_formatter_omits_empty_fields() -> None:
    formatted = format_browser_diagnostic(
        {
            "error_type": "PAGE_LOAD_TIMEOUT",
            "user_message": "Dola page did not finish loading.",
            "stage": "page_loading",
            "timeout_seconds": 120,
            "error_code": None,
            "error_msg": "",
            "visible_elements": ["loading_skeleton"],
        }
    )

    assert "Error type: PAGE_LOAD_TIMEOUT" in formatted
    assert "Timeout: 120s" in formatted
    assert "loading_skeleton" in formatted
    assert "None" not in formatted
    assert "Dola error:" not in formatted


@pytest.mark.asyncio
async def test_page_ready_waits_for_video_tab_instead_of_failing_on_skeleton(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeVideoTab:
        def __init__(self) -> None:
            self.checks = 0

        async def count(self) -> int:
            self.checks += 1
            return 1 if self.checks >= 3 else 0

        def nth(self, _index: int) -> "FakeVideoTab":
            return self

        async def is_visible(self) -> bool:
            return True

    class FakePage:
        url = "https://www.dola.com/chat/create-image"

        def __init__(self) -> None:
            self.video_tab = FakeVideoTab()

        def get_by_role(self, _role: str, **_kwargs: object) -> FakeVideoTab:
            return self.video_tab

    client = DolaBrowserClient(proxy_url="http://proxy.example.com:2312")
    page = FakePage()
    network = BrowserNetworkState()
    logs: list[str] = []

    async def no_block(_page: object, _network: BrowserNetworkState) -> None:
        return None

    async def visible(_page: object) -> list[str]:
        return ["loading_skeleton"]

    async def no_sleep(_seconds: float) -> None:
        return None

    async def no_dismiss(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(client, "_raise_if_blocked", no_block)
    monkeypatch.setattr(client, "_visible_dola_elements", visible)
    monkeypatch.setattr(client, "_dismiss_cookie_banner", no_dismiss)
    monkeypatch.setattr(client, "_dismiss_login_popup", no_dismiss)
    monkeypatch.setattr(dola_browser.asyncio, "sleep", no_sleep)

    await client._ensure_dola_ready(page, network, lambda message, _level: logs.append(message))  # type: ignore[arg-type]

    assert network.last_successful_stage == "page_ready"
    assert network.visible_elements == ["loading_skeleton"]
    assert logs and logs[0].startswith("Waiting for Dola page")
    assert dola_browser.PROXY_PAGE_TIMEOUT_SECONDS == 120


@pytest.mark.asyncio
async def test_submit_button_waits_until_candidate_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeButton:
        def __init__(self) -> None:
            self.enabled_checks = 0

        async def is_visible(self) -> bool:
            return True

        async def is_enabled(self) -> bool:
            self.enabled_checks += 1
            return self.enabled_checks >= 3

    class EmptyLocator:
        async def count(self) -> int:
            return 0

    class FakePage:
        def locator(self, _selector: str) -> EmptyLocator:
            return EmptyLocator()

    class FakeTextbox:
        pass

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(dola_browser.asyncio, "sleep", no_sleep)
    client = DolaBrowserClient()
    button = FakeButton()

    result = await client._wait_for_submit_button(FakePage(), FakeTextbox(), 1, button)  # type: ignore[arg-type]

    assert result is button
    assert button.enabled_checks == 3


@pytest.mark.asyncio
async def test_submit_candidate_uses_stable_send_wrapper() -> None:
    class FakeButton:
        async def count(self) -> int:
            return 1

        def nth(self, _index: int) -> "FakeLocator":
            return self

        def nth(self, _index: int) -> "FakeButton":
            return self

        async def is_visible(self) -> bool:
            return True

    class EmptyButton(FakeButton):
        async def count(self) -> int:
            return 0

    class FakePage:
        def __init__(self) -> None:
            self.requested: list[str] = []
            self.send = FakeButton()

        def locator(self, selector: str) -> FakeButton:
            self.requested.append(selector)
            return self.send if selector == ".send-btn-wrapper > button" else EmptyButton()

    page = FakePage()
    client = DolaBrowserClient()

    result = await client._find_submit_button_candidate(page, object())  # type: ignore[arg-type]

    assert result is page.send
    assert page.requested[0] == ".send-btn-wrapper > button"


@pytest.mark.asyncio
async def test_submit_clicks_stable_button_once_without_pressing_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeInput:
        def __init__(self) -> None:
            self.text = ""

        async def click(self, **_kwargs: object) -> None:
            pass

        async def inner_text(self) -> str:
            return self.text

    class FakeButton:
        def __init__(self) -> None:
            self.clicks = 0

        async def click(self, **_kwargs: object) -> None:
            self.clicks += 1

    class FakeKeyboard:
        def __init__(self, input_locator: FakeInput) -> None:
            self.input = input_locator
            self.pressed: list[str] = []

        async def press(self, key: str) -> None:
            self.pressed.append(key)
            if key == "Backspace":
                self.input.text = ""

        async def insert_text(self, text: str) -> None:
            self.input.text = text

        async def type(self, text: str, **_kwargs: object) -> None:
            self.input.text += text

    class FakePage:
        def __init__(self, input_locator: FakeInput) -> None:
            self.keyboard = FakeKeyboard(input_locator)

    client = DolaBrowserClient()
    input_locator = FakeInput()
    button = FakeButton()
    page = FakePage(input_locator)
    logs: list[str] = []

    async def noop(_page: object) -> None:
        pass

    async def no_login(*_args: object, **_kwargs: object) -> bool:
        return False

    async def find_input(_page: object, _timeout: int) -> FakeInput:
        return input_locator

    async def find_button(_page: object, _textbox: object) -> FakeButton:
        return button

    async def wait_button(_page: object, _textbox: object, _timeout: int, _candidate: object) -> FakeButton:
        return button

    monkeypatch.setattr(client, "_dismiss_cookie_banner", noop)
    monkeypatch.setattr(client, "_dismiss_login_popup", no_login)
    monkeypatch.setattr(client, "_dismiss_modal_overlays", noop)
    monkeypatch.setattr(client, "_wait_for_video_textbox", find_input)
    monkeypatch.setattr(client, "_find_submit_button_candidate", find_button)
    monkeypatch.setattr(client, "_wait_for_submit_button", wait_button)

    await client._submit_via_ui(page, "test prompt", BrowserNetworkState(), lambda message, _level: logs.append(message))  # type: ignore[arg-type]

    assert button.clicks == 1
    assert "Enter" not in page.keyboard.pressed
    assert logs == ["Entering prompt", "Prompt verified", "Waiting for submit button", "Submitting prompt"]


@pytest.mark.asyncio
async def test_video_mode_ready_without_selected_tab_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeLocator:
        def __init__(self) -> None:
            self.clicks = 0

        async def count(self) -> int:
            return 1

        async def is_visible(self) -> bool:
            return True

        def nth(self, _index: int) -> "FakeLocator":
            return self

        async def click(self, **_kwargs: object) -> None:
            self.clicks += 1

    class FakePage:
        def __init__(self) -> None:
            self.video = FakeLocator()
            self.model = FakeLocator()
            self.ratio = FakeLocator()

        def get_by_role(self, role: str, **kwargs: object) -> FakeLocator:
            name = kwargs.get("name")
            if role == "tab" and name == "Video":
                return self.video
            if role == "button" and name == "Seedance 2.0 Fast":
                return self.model
            if role == "button" and name == "Ratio":
                return self.ratio
            raise AssertionError(f"Unexpected role lookup: {role} {name}")

    client = DolaBrowserClient()
    page = FakePage()
    network = BrowserNetworkState()
    logs: list[str] = []

    async def no_popup(*_args: object, **_kwargs: object) -> bool:
        return False

    async def no_block(*_args: object, **_kwargs: object) -> None:
        return None

    async def textbox(_page: object) -> object:
        return object()

    async def duration(_page: object) -> bool:
        return True

    monkeypatch.setattr(client, "_dismiss_login_popup", no_popup)
    monkeypatch.setattr(client, "_raise_if_blocked", no_block)
    monkeypatch.setattr(client, "_find_video_textbox", textbox)
    monkeypatch.setattr(client, "_duration_control_visible", duration)

    await client._select_video_mode(page, network, lambda message, _level: logs.append(message))  # type: ignore[arg-type]

    assert page.video.clicks == 1
    assert network.last_successful_stage == "video_mode_ready"
    assert network.video_tab_visible is True
    assert network.model_visible is True
    assert network.duration_visible is True
    assert network.ratio_visible is True
    assert network.textbox_visible is True
    assert logs == ["Video tab clicked", "Video controls ready"]


@pytest.mark.asyncio
async def test_visible_control_is_selected_from_hidden_duplicate_dom() -> None:
    class Candidate:
        def __init__(self, visible: bool) -> None:
            self.visible = visible

        async def is_visible(self) -> bool:
            return self.visible

    class DuplicateLocator:
        def __init__(self) -> None:
            self.candidates = [Candidate(False), Candidate(True)]

        async def count(self) -> int:
            return len(self.candidates)

        def nth(self, index: int) -> Candidate:
            return self.candidates[index]

    class FakePage:
        def __init__(self) -> None:
            self.duration = DuplicateLocator()

        def get_by_role(self, _role: str, **kwargs: object) -> DuplicateLocator:
            if kwargs.get("name") == "5s":
                return self.duration
            return DuplicateLocator()

    client = DolaBrowserClient()
    locator = DuplicateLocator()

    visible = await client._first_visible(locator)  # type: ignore[arg-type]

    assert visible is locator.candidates[1]
    assert await client._duration_control_visible(FakePage()) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ratio_verification_accepts_dola_ratio_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeLocator:
        def __init__(self, present: bool) -> None:
            self.present = present

        async def count(self) -> int:
            return int(self.present)

        def nth(self, _index: int) -> "FakeLocator":
            return self

        async def is_visible(self) -> bool:
            return self.present

        async def inner_text(self) -> str:
            return "Ratio 9:16"

    class FakePage:
        def get_by_role(self, role: str, **kwargs: object) -> FakeLocator:
            return FakeLocator(role == "button" and kwargs.get("name") == "Ratio 9:16")

    client = DolaBrowserClient()
    network = BrowserNetworkState()

    async def no_popup(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(client, "_dismiss_login_popup", no_popup)

    await client._select_ratio(FakePage(), "9:16", network, None)  # type: ignore[arg-type]

    assert network.selected_ratio == "9:16"


@pytest.mark.asyncio
async def test_closeable_login_popup_is_dismissed(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClose:
        def __init__(self) -> None:
            self.clicks = 0

        async def count(self) -> int:
            return 1

        def nth(self, _index: int) -> "FakeClose":
            return self

        async def is_visible(self) -> bool:
            return True

        async def click(self, **_kwargs: object) -> None:
            self.clicks += 1

    class EmptyClose(FakeClose):
        async def count(self) -> int:
            return 0

    class FakeKeyboard:
        async def press(self, _key: str) -> None:
            pass

    class FakePage:
        def __init__(self) -> None:
            self.close = FakeClose()
            self.keyboard = FakeKeyboard()

        def locator(self, selector: str) -> FakeClose:
            return self.close if selector == "[role='dialog'] .semi-modal-close" else EmptyClose()

    client = DolaBrowserClient()
    visible_states = iter([True, False])
    logs: list[tuple[str, str]] = []

    async def popup_visible(_page: object) -> bool:
        return next(visible_states)

    monkeypatch.setattr(client, "_login_popup_visible", popup_visible)
    page = FakePage()

    closed = await client._dismiss_login_popup(page, BrowserNetworkState(), lambda message, level: logs.append((message, level)))  # type: ignore[arg-type]

    assert closed is True
    assert page.close.clicks == 1
    assert logs == [
        ("Dola login popup detected", "warn"),
        ("Dola login popup closed", "success"),
        ("Continuing Dola page loading", "info"),
    ]


@pytest.mark.asyncio
async def test_uncloseable_login_popup_fails_after_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyClose:
        async def count(self) -> int:
            return 0

    class FakeKeyboard:
        def __init__(self) -> None:
            self.presses = 0

        async def press(self, _key: str) -> None:
            self.presses += 1

    class FakePage:
        url = "https://www.dola.com/chat/create-image"

        def __init__(self) -> None:
            self.keyboard = FakeKeyboard()

        def locator(self, _selector: str) -> EmptyClose:
            return EmptyClose()

    client = DolaBrowserClient()

    async def always_visible(_page: object) -> bool:
        return True

    async def no_screenshot(_page: object, _name: str) -> str:
        return ""

    ticks = iter(range(0, 100, 3))
    monkeypatch.setattr(client, "_login_popup_visible", always_visible)
    monkeypatch.setattr(client, "_screenshot", no_screenshot)
    monkeypatch.setattr(dola_browser.time, "monotonic", lambda: next(ticks))
    page = FakePage()

    with pytest.raises(DolaBrowserError) as exc_info:
        await client._dismiss_login_popup(page, BrowserNetworkState())  # type: ignore[arg-type]

    assert exc_info.value.error_type == "LOGIN_REQUIRED"
    assert "three attempts" in str(exc_info.value)
    assert page.keyboard.presses == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", ["9:16", "16:9", "1:1"])
@pytest.mark.parametrize(("duration", "ui_duration"), [(5, 5), (10, 10), (15, 10)])
async def test_generation_options_select_exact_ratio_and_duration(
    monkeypatch: pytest.MonkeyPatch,
    ratio: str,
    duration: int,
    ui_duration: int,
) -> None:
    client = DolaBrowserClient()
    network = BrowserNetworkState()
    calls: list[tuple[str, object]] = []

    async def select_ratio(_page: object, value: str, _network: BrowserNetworkState, _log_fn: object) -> None:
        calls.append(("ratio", value))
        _network.selected_ratio = value

    async def select_duration(_page: object, value: int, _network: BrowserNetworkState, _log_fn: object) -> None:
        calls.append(("duration", value))
        _network.selected_duration = value

    monkeypatch.setattr(client, "_select_ratio", select_ratio)
    monkeypatch.setattr(client, "_select_duration", select_duration)

    await client._select_generation_options(object(), duration, ratio, network)  # type: ignore[arg-type]

    assert calls == [("ratio", ratio), ("duration", ui_duration)]
    assert network.requested_duration == duration
    assert network.requested_ratio == ratio
    assert network.selected_duration == ui_duration


@pytest.mark.asyncio
async def test_captured_generation_option_mismatch_fails_before_session_build(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DolaBrowserClient()
    network = BrowserNetworkState(
        conversation_id="123456789",
        captured_url="https://www.dola.com/chat/completion",
        requested_duration=15,
        requested_ratio="9:16",
        captured_duration=10,
        captured_ratio="9:16",
    )

    class FakePage:
        url = "https://www.dola.com/chat/123456789"

    async def no_screenshot(_page: object, _name: str) -> str:
        return ""

    monkeypatch.setattr(client, "_screenshot", no_screenshot)

    with pytest.raises(DolaBrowserError) as exc_info:
        await client._wait_for_submit_capture(object(), FakePage(), network)  # type: ignore[arg-type]

    assert exc_info.value.error_type == "GENERATION_OPTIONS_MISMATCH"
    assert "requested 15s 9:16, captured 10s 9:16" in str(exc_info.value)


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

        async def post(self, url: str, json: dict[str, object]) -> FakeResponse:
            assert url.endswith("/close")
            assert json == {"slot_id": "slot-1", "delete_profile": True}
            return FakeResponse()

    monkeypatch.setattr(dola_browser.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient())
    client = DolaBrowserClient(manager_url="http://browser-manager:7070")
    fake_playwright = FakePlaywright()
    client._active_slots["slot-1"] = {"playwright": fake_playwright}

    assert await client.close_slot("slot-1") is True
    assert fake_playwright.stopped is True
    assert "slot-1" not in client._active_slots


@pytest.mark.asyncio
async def test_close_slot_can_keep_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePlaywright:
        async def stop(self) -> None:
            return None

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

        async def post(self, _url: str, json: dict[str, object]) -> FakeResponse:
            assert json == {"slot_id": "slot-1", "delete_profile": False}
            return FakeResponse()

    monkeypatch.setattr(dola_browser.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient())
    client = DolaBrowserClient(manager_url="http://browser-manager:7070")
    client._active_slots["slot-1"] = {"playwright": FakePlaywright()}

    assert await client.close_slot("slot-1", delete_profile=False) is True


@pytest.mark.asyncio
async def test_delete_profile_calls_browser_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, bool]:
            return {"deleted": True}

    class FakeAsyncClient:
        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def post(self, url: str, json: dict[str, object]) -> FakeResponse:
            assert url.endswith("/delete-profile")
            assert json == {"profile_dir": "/data/browser-profile/slots/slot-1"}
            return FakeResponse()

    monkeypatch.setattr(dola_browser.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient())
    client = DolaBrowserClient(manager_url="http://browser-manager:7070")

    assert await client.delete_profile("/data/browser-profile/slots/slot-1") is True


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

    with pytest.raises(DolaBrowserError, match="could not capture the submitted conversation") as exc_info:
        await client._wait_for_submit_capture(None, FakePage(), network)  # type: ignore[arg-type]
    assert exc_info.value.error_type == "SUBMIT_NOT_CAPTURED"


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

    async def fake_select_video(_page: FakePage, _network: BrowserNetworkState, _log_fn: object) -> None:
        events.append("select-video")

    async def fake_select_options(_page: FakePage, _duration: int, _ratio: str, _network: BrowserNetworkState, _log_fn: object) -> None:
        events.append("select-options")

    async def fake_submit(_page: FakePage, prompt: str, _network: BrowserNetworkState, _log_fn: object) -> None:
        events.append("submit")
        submitted_prompts.append(prompt)

    async def fake_wait(_context: FakeContext, _page: FakePage, _network: BrowserNetworkState, _log_fn: object) -> DolaBrowserSubmitResult:
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
    monkeypatch.setattr(client, "_select_generation_options", fake_select_options)
    monkeypatch.setattr(client, "_submit_via_ui", fake_submit)
    monkeypatch.setattr(client, "_wait_for_submit_capture", fake_wait)

    result = await client.submit_and_capture_session("cinematic city", 15, "9:16")

    assert result.slot_id == "slot-1"
    assert events == ["hook", "goto", "select-video", "select-options", "submit"]
    assert context.pages[0].url == "https://www.dola.com/chat/create-image"
    assert submitted_prompts == ["Generate video: cinematic city"]


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

    async def no_login(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(client, "_slot_page", fake_slot_page)
    monkeypatch.setattr(client, "_click_ready_video_card", fake_click_ready)
    monkeypatch.setattr(client, "_video_src_from_dom", fake_video_src)
    monkeypatch.setattr(client, "_dismiss_login_popup", no_login)

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

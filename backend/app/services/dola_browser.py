from __future__ import annotations

import asyncio
import json
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import httpx
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, async_playwright

from app.config import settings
from app.services.dola import (
    DolaSession,
    base_payload,
    build_video_prompt_text,
    cookie_names_from_header,
    format_cookie_header,
    parse_conversation_from_stream,
    parse_dola_stream_error,
    parse_play_info,
    sanitize_dola_log_message,
)


DOLA_HOME_URL = "https://www.dola.com/"
DOLA_CREATE_IMAGE_URL = "https://www.dola.com/chat/create-image"
DOLA_CHAT_URL = "https://www.dola.com/chat/"
BROWSER_TIMEOUT_MS = 45_000
BROWSER_GENERATION_TIMEOUT_SECONDS = 900
BROWSER_SUBMIT_CAPTURE_TIMEOUT_SECONDS = 90
CHAT_INPUT_SELECTORS = (
    "textarea",
    "[contenteditable='true']",
    "[role='textbox']",
    "div.ProseMirror",
    "div[placeholder]",
)
VIDEO_MODE_SELECTORS = (
    "button:has-text('Video')",
    "div[role='tab']:has-text('Video')",
    "span:has-text('Video')",
    "text='Video'",
)
@dataclass
class BrowserNetworkState:
    conversation_id: str | None = None
    conversation_type: int = 3
    vid: str | None = None
    download_url: str | None = None
    error_code: int | None = None
    error_msg: str = ""
    last_endpoint: str = ""
    last_status: int | None = None
    last_body_snippet: str = ""
    captured_request: dict[str, Any] = field(default_factory=dict)
    captured_headers: dict[str, str] = field(default_factory=dict)
    captured_url: str = ""
    captured_method: str = ""


@dataclass
class DolaBrowserSubmitResult:
    session: DolaSession
    conversation_id: str
    conversation_type: int
    diagnostic: dict[str, Any]
    slot_id: str = ""


@dataclass
class DolaBrowserDownloadResult:
    download_url: str
    vid: str
    diagnostic: dict[str, Any]


class DolaBrowserError(RuntimeError):
    def __init__(self, message: str, diagnostic: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class DolaBrowserClient:
    def __init__(
        self,
        cdp_url: str = "",
        manual_url: str = "",
        screenshot_dir: Path | None = None,
        proxy_url: str = "",
        manager_url: str = "",
    ) -> None:
        self.cdp_url = cdp_url or settings.dola_browser_cdp_url
        self.manager_url = manager_url or settings.dola_browser_manager_url or self.cdp_url
        self.manual_url = manual_url or settings.dola_browser_manual_url
        self.screenshot_dir = screenshot_dir or (settings.log_dir / "dola-browser")
        self.proxy_url = proxy_url.strip()
        self._active_slots: dict[str, dict[str, Any]] = {}
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> BrowserContext:
        slot = await self._launch_slot()
        runtime = await self._connect_slot(slot)
        self._active_slots[slot["slot_id"]] = runtime
        return runtime["context"]

    async def _launch_slot(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{resolve_cdp_url(self.manager_url).rstrip('/')}/launch", json={"proxy_url": self.proxy_url})
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise DolaBrowserError(f"Dola browser manager launch failed: {payload.get('error')}", {"cdp": False, "error_msg": payload.get("error")})
        return payload["slot"]

    async def _connect_slot(self, slot: dict[str, Any]) -> dict[str, Any]:
        playwright = await async_playwright().start()
        try:
            cdp_url = resolve_cdp_url(str(slot.get("container_cdp_url") or slot.get("cdp_url")))
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            return {"slot": slot, "playwright": playwright, "browser": browser, "context": context}
        except Exception:
            await playwright.stop()
            await self.close_slot(str(slot.get("slot_id") or ""))
            raise

    async def close_slot(self, slot_id: str) -> bool:
        runtime = self._active_slots.pop(slot_id, None)
        if runtime:
            try:
                await runtime["playwright"].stop()
            except Exception:
                pass
        if slot_id:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(f"{resolve_cdp_url(self.manager_url).rstrip('/')}/close", json={"slot_id": slot_id})
                    response.raise_for_status()
                    payload = response.json()
                    return bool(payload.get("closed"))
            except Exception:
                return False
        return False

    async def _slot_context(self, slot_id: str) -> BrowserContext:
        runtime = self._active_slots.get(slot_id)
        if not runtime:
            raise DolaBrowserError("Dola browser slot is no longer active.", {"cdp": False, "slot_id": slot_id})
        return runtime["context"]

    async def _slot_page(self, slot_id: str, conversation_id: str) -> Page:
        context = await self._slot_context(slot_id)
        return await self._page_for_conversation(context, conversation_id)

    async def _legacy_connect_unused(self) -> BrowserContext:
        async with self._connect_lock:
            return await self.connect()

    async def close(self) -> None:
        for slot_id in list(self._active_slots):
            await self.close_slot(slot_id)

    async def status(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                manager_url = resolve_cdp_url(self.manager_url).rstrip("/")
                status_response = await client.get(f"{manager_url}/status")
                status_response.raise_for_status()
            manager_status = status_response.json()
            browser_ip = ""
            return {
                "ok": True,
                "cdp": True,
                "page_url": "browser manager ready",
                "profile_persistent": True,
                "manual_url": self.manual_url,
                "mode": settings.dola_mode,
                "browser_proxy_active": bool(self.proxy_url),
                "browser_proxy_host": proxy_public_host(self.proxy_url),
                "browser_ip": browser_ip,
                "page_count": manager_status.get("active_browser_count", 0),
                "active_browser_count": manager_status.get("active_browser_count", 0),
                "max_browser_slots": manager_status.get("max_browser_slots", 0),
                "active_cdp_ports": manager_status.get("active_cdp_ports", []),
                "last_submit_endpoint": "",
                "last_dola_error": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "cdp": False,
                "page_url": "",
                "profile_persistent": True,
                "manual_url": self.manual_url,
                "mode": settings.dola_mode,
                "error": str(exc),
                "browser_proxy_active": bool(self.proxy_url),
                "browser_proxy_host": proxy_public_host(self.proxy_url),
                "browser_ip": "",
                "page_count": 0,
                "last_submit_endpoint": "",
                "last_dola_error": str(exc),
            }

    async def _browser_ip(self, context: BrowserContext) -> str:
        page = await context.new_page()
        try:
            await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=15_000)
            body = await page.locator("body").inner_text(timeout=5_000)
            payload = json_loads_or_empty(body)
            return str(payload.get("ip") or "")
        finally:
            await page.close()

    async def submit_and_capture_session(self, prompt: str, duration: int, ratio: str, *, log_fn: Any | None = None) -> DolaBrowserSubmitResult:
        slot = await self._launch_slot()
        slot_id = str(slot["slot_id"])
        if log_fn:
            log_fn(f"Launching browser slot {slot.get('slot_number')}", "info")
        page: Page | None = None
        network = BrowserNetworkState()
        try:
            runtime = await self._connect_slot(slot)
            self._active_slots[slot_id] = runtime
            if log_fn:
                log_fn(f"Browser slot {slot.get('slot_number')} connected", "info")
            context = runtime["context"]
            page = await self.new_job_page(context)
            page.on("request", lambda request: self._capture_request(request, network))
            page.on("response", lambda response: asyncio.create_task(self._capture_response(response, network)))
            if log_fn:
                log_fn(f"Submitting through browser slot {slot.get('slot_number')}", "info")
            await self._ensure_dola_ready(page)
            await self._raise_if_blocked(page, network)
            await self._select_video_mode(page)
            full_prompt = build_browser_video_prompt_text(prompt, int(duration), ratio)
            if log_fn:
                log_fn("Submitting prompt through Dola browser page.", "info")
            await self._submit_via_ui(page, full_prompt)
            result = await self._wait_for_submit_capture(context, page, network)
            result.slot_id = slot_id
            result.diagnostic["slot_id"] = slot_id
            result.diagnostic["slot_number"] = slot.get("slot_number")
            if log_fn:
                log_fn("Captured browser session", "success")
            return result
        except DolaBrowserError:
            await self._close_failed_submit_slot(slot_id, log_fn)
            raise
        except Exception as exc:
            screenshot_path = await self._screenshot(page, "browser-failure") if page else ""
            await self._close_failed_submit_slot(slot_id, log_fn)
            raise DolaBrowserError(
                f"Dola browser failed: {exc}",
                self._diagnostic(page, network, screenshot_path=screenshot_path),
            ) from exc

    async def _close_failed_submit_slot(self, slot_id: str, log_fn: Any | None = None) -> None:
        if not slot_id:
            return
        if log_fn:
            log_fn("Deleting browser profile after rejection", "info")
        closed = await self.close_slot(slot_id)
        if log_fn:
            log_fn("Browser profile cleaned" if closed else f"Browser cleanup failed: {slot_id}", "success" if closed else "error")

    async def wait_for_download_from_ready_card(
        self,
        conversation_id: str,
        *,
        slot_id: str = "",
        log_fn: Any | None = None,
        timeout_seconds: int = 240,
    ) -> DolaBrowserDownloadResult:
        page = await self._slot_page(slot_id, conversation_id) if slot_id else await self._page_for_conversation(await self.connect(), conversation_id)
        network = BrowserNetworkState(conversation_id=conversation_id)
        page.on("response", lambda response: asyncio.create_task(self._capture_response(response, network)))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if network.error_code:
                raise DolaBrowserError(self._message_for_error(network), self._diagnostic(page, network))
            direct_src = await self._video_src_from_dom(page)
            if direct_src:
                return DolaBrowserDownloadResult(
                    download_url=direct_src,
                    vid=vid_from_download_url(direct_src) or f"browser-{conversation_id[-8:]}",
                    diagnostic=self._diagnostic(page, network),
                )
            if await self._click_ready_video_card(page):
                if log_fn:
                    log_fn("Opened ready Dola video card to capture play_info.", "info")
                await page.wait_for_timeout(3_000)
                direct_src = await self._video_src_from_dom(page)
                if direct_src:
                    return DolaBrowserDownloadResult(
                        download_url=direct_src,
                        vid=vid_from_download_url(direct_src) or f"browser-{conversation_id[-8:]}",
                        diagnostic=self._diagnostic(page, network),
                    )
                if network.download_url:
                    return DolaBrowserDownloadResult(
                        download_url=network.download_url,
                        vid=network.vid or vid_from_download_url(network.download_url) or f"browser-{conversation_id[-8:]}",
                        diagnostic=self._diagnostic(page, network),
                    )
            await asyncio.sleep(3)
        screenshot_path = await self._screenshot(page, "download-not-found")
        raise DolaBrowserError("Dola browser did not expose play_info URL.", self._diagnostic(page, network, screenshot_path=screenshot_path))

    async def _page(self, context: BrowserContext) -> Page:
        for page in context.pages:
            if "dola.com" in page.url:
                return page
        page = await context.new_page()
        await page.goto(DOLA_HOME_URL, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        return page

    async def new_job_page(self, context: BrowserContext | None = None) -> Page:
        context = context or await self.connect()
        page = await context.new_page()
        await page.goto(DOLA_CREATE_IMAGE_URL, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        return page

    async def _page_for_conversation(self, context: BrowserContext, conversation_id: str) -> Page:
        for page in context.pages:
            if conversation_id in page.url:
                return page
        page = await self._page(context)
        await page.goto(f"{DOLA_CHAT_URL}{conversation_id}", wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        return page

    async def _ensure_dola_ready(self, page: Page) -> None:
        if DOLA_CREATE_IMAGE_URL not in page.url:
            await page.goto(DOLA_CREATE_IMAGE_URL, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightError:
            pass

    async def _raise_if_blocked(self, page: Page, network: BrowserNetworkState) -> None:
        body_text = sanitize_dola_log_message((await page.locator("body").inner_text(timeout=10_000))[:2000])
        lowered = body_text.lower()
        if "not available in your country" in lowered or "country/region" in lowered:
            raise DolaBrowserError("Dola browser country/region restricted.", self._diagnostic(page, network, body_snippet=body_text))
        if "captcha" in lowered or "verify" in lowered:
            raise DolaBrowserError("Dola browser requires manual verification.", self._diagnostic(page, network, body_snippet=body_text))
        if "log in" in lowered and "chat" not in lowered:
            raise DolaBrowserError("Dola browser requires manual login/action.", self._diagnostic(page, network, body_snippet=body_text))

    async def _submit_via_ui(self, page: Page, prompt: str) -> None:
        await self._dismiss_cookie_banner(page)
        input_locator = None
        for selector in CHAT_INPUT_SELECTORS:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    candidate = locator.nth(index)
                    if await candidate.is_visible(timeout=2_000) and await candidate.is_enabled(timeout=2_000):
                        input_locator = candidate
                        break
                if input_locator:
                    break
            except PlaywrightError:
                continue
        if input_locator is None:
            screenshot_path = await self._screenshot(page, "input-not-found")
            raise DolaBrowserError("Dola chat input not found.", self._diagnostic(page, BrowserNetworkState(), screenshot_path=screenshot_path))

        await input_locator.click(timeout=10_000)
        try:
            await input_locator.fill(prompt, timeout=5_000)
        except PlaywrightError:
            await page.keyboard.press("Control+A")
            await page.keyboard.type(prompt, delay=1)

        send_candidates = (
            "button[type='submit']",
            "button:has-text('Send')",
            "button:has-text('Generate')",
            "[aria-label*='send' i]",
            "[aria-label*='submit' i]",
        )
        for selector in send_candidates:
            button = page.locator(selector).last
            try:
                if await button.count() and await button.is_visible(timeout=1_500) and await button.is_enabled(timeout=1_500):
                    await button.click(timeout=5_000)
                    return
            except PlaywrightError:
                continue
        await page.keyboard.press("Enter")

    async def _select_video_mode(self, page: Page) -> None:
        for selector in VIDEO_MODE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=2_000):
                    await locator.click(force=True, timeout=5_000)
                    await page.wait_for_timeout(1_000)
                    return
            except PlaywrightError:
                continue

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        for selector in ("button:has-text('OK')", "button:has-text('Accept')", "button:has-text('Agree')"):
            button = page.locator(selector).first
            try:
                if await button.count() and await button.is_visible(timeout=1_000):
                    await button.click(timeout=3_000)
                    return
            except PlaywrightError:
                continue

    async def _click_ready_video_card(self, page: Page) -> bool:
        body = (await page.locator("body").inner_text(timeout=5_000)).lower()
        if "your video is ready" not in body and not await page.locator("img[class*=cover]").count():
            return False
        for selector in (".video-player-wrapper-IZ7Zoq", "[class*=video-player-wrapper]", "[class*=play-icon-wrapper]", "[class*=block-video]", "img[class*=cover]"):
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible(timeout=1_000):
                    await locator.click(force=True, timeout=5_000)
                    return True
            except PlaywrightError:
                continue
        return False

    async def _video_src_from_dom(self, page: Page) -> str:
        video = page.locator("video[src]").first
        try:
            if await video.count():
                src = await video.get_attribute("src")
                return src or ""
        except PlaywrightError:
            return ""
        return ""

    async def _wait_for_submit_capture(self, context: BrowserContext, page: Page, network: BrowserNetworkState) -> DolaBrowserSubmitResult:
        deadline = time.monotonic() + BROWSER_SUBMIT_CAPTURE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if network.error_code:
                raise DolaBrowserError(self._message_for_error(network), self._diagnostic(page, network))
            conversation_id = network.conversation_id or conversation_id_from_url(page.url)
            if conversation_id and network.captured_url:
                session = await self._build_dola_session_from_browser(context, network)
                if not network.conversation_id:
                    network.conversation_id = conversation_id
                return DolaBrowserSubmitResult(
                    session=session,
                    conversation_id=conversation_id,
                    conversation_type=network.conversation_type,
                    diagnostic=self._diagnostic(page, network),
                )
            await asyncio.sleep(2)
        raise DolaBrowserError("Dola browser submitted but conversation_id was not captured.", self._diagnostic(page, network))

    async def _build_dola_session_from_browser(self, context: BrowserContext, network: BrowserNetworkState) -> DolaSession:
        cookies = await context.cookies("https://www.dola.com")
        cookie_map = {cookie["name"]: cookie["value"] for cookie in cookies if cookie.get("name") and cookie.get("value")}
        cookie_header = format_cookie_header(cookie_map)
        headers = sanitize_replay_headers(network.captured_headers)
        headers["cookie"] = cookie_header
        headers.setdefault("accept", "*/*")
        headers.setdefault("origin", "https://www.dola.com")
        headers.setdefault("referer", "https://www.dola.com/chat/")
        headers.setdefault("content-type", "application/json")
        user_agent = headers.get("user-agent") or await context.pages[0].evaluate("navigator.userAgent")
        headers["user-agent"] = user_agent
        fp = fp_from_url_or_cookies(network.captured_url, cookie_map)
        return DolaSession(
            url=network.captured_url,
            headers=headers,
            payload_template=base_payload(fp),
            fp=fp,
            has_ttwid=bool(cookie_map.get("ttwid")),
            has_hook_slardar=bool(cookie_map.get("hook_slardar_session_id")),
            has_auth_cookies=has_auth_cookie(cookie_map),
        )

    def _capture_request(self, request: Any, network: BrowserNetworkState) -> None:
        if "/chat/completion" not in request.url:
            return
        network.captured_url = request.url
        network.captured_method = request.method
        network.captured_headers = dict(request.headers)
        network.captured_request = {
            "url": request.url,
            "method": request.method,
            "headers": redact_headers(request.headers),
            "post_data_snippet": sanitize_dola_log_message((request.post_data or "")[:500]),
        }

    async def _capture_response(self, response: Any, network: BrowserNetworkState) -> None:
        endpoint = endpoint_name(response.url)
        if not endpoint:
            return
        network.last_endpoint = endpoint
        network.last_status = response.status
        try:
            text = await response.text()
        except PlaywrightError:
            return
        network.last_body_snippet = sanitize_dola_log_message(text[:500].replace("\n", " "))
        error_code, error_msg = parse_dola_stream_error(text)
        if error_code:
            network.error_code = error_code
            network.error_msg = error_msg
            return
        if endpoint == "submit":
            try:
                network.conversation_id, network.conversation_type = parse_conversation_from_stream(text)
            except ValueError:
                pass
        elif endpoint == "play_info":
            try:
                payload = json_loads_or_empty(text)
            except ValueError:
                payload = {}
            download_url = parse_play_info(payload)
            if download_url:
                network.download_url = download_url
                network.vid = vid_from_download_url(download_url)

    def _message_for_error(self, network: BrowserNetworkState) -> str:
        if network.error_code == 710022002:
            return "Dola browser returned high demand."
        if network.error_code == 710022017:
            return "Dola browser country/region restricted."
        return f"Dola browser returned error {network.error_code}: {network.error_msg}"

    async def _screenshot(self, page: Page, name: str) -> str:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / f"{name}-{int(time.time())}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except PlaywrightError:
            return ""

    def _diagnostic(
        self,
        page: Page,
        network: BrowserNetworkState,
        *,
        screenshot_path: str = "",
        body_snippet: str = "",
    ) -> dict[str, Any]:
        return {
            "cdp": True,
            "page_url": page.url,
            "manual_url": self.manual_url,
            "endpoint": network.last_endpoint,
            "status_code": network.last_status,
            "error_code": network.error_code,
            "error_msg": network.error_msg,
            "body_snippet": body_snippet or network.last_body_snippet,
            "conversation_id": mask_id(network.conversation_id),
            "vid": mask_id(network.vid),
            "has_download_url": bool(network.download_url),
            "captured_request": network.captured_request,
            "captured_endpoint": endpoint_name(network.captured_url),
            "cookie_names": cookie_names_from_header(network.captured_headers.get("cookie", "")),
            "screenshot_path": screenshot_path,
            "browser_proxy_active": bool(self.proxy_url),
            "browser_proxy_host": proxy_public_host(self.proxy_url),
        }


def endpoint_name(url: str) -> str:
    if "/chat/completion" in url:
        return "submit"
    if "/im/chain/single" in url:
        return "chain_poll"
    if "/samantha/video/get_play_info" in url:
        return "play_info"
    return ""


def json_loads_or_empty(text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON") from exc
    return loaded if isinstance(loaded, dict) else {}


def build_browser_video_prompt_text(prompt: str, duration: int, ratio: str) -> str:
    return build_video_prompt_text(prompt, int(duration), ratio)


def resolve_cdp_url(cdp_url: str) -> str:
    parsed = urlparse(cdp_url)
    if parsed.hostname in {None, "localhost", "127.0.0.1", "::1"}:
        return cdp_url
    try:
        host = socket.gethostbyname(parsed.hostname)
    except OSError:
        return cdp_url
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in {"cookie", "authorization", "x-api-key"}:
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


UNSAFE_REPLAY_HEADERS = {
    "accept-encoding",
    "content-length",
    "connection",
    "host",
    "cookie",
    "origin-policy",
    "priority",
}


def sanitize_replay_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in UNSAFE_REPLAY_HEADERS}


def conversation_id_from_url(url: str) -> str | None:
    match = re.search(r"/chat/([0-9]{8,})", url)
    return match.group(1) if match else None


def fp_from_url_or_cookies(url: str, cookies: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(part.split("=", 1) for part in parsed.query.split("&") if "=" in part)
    return query.get("fp") or cookies.get("s_v_web_id") or ""


def has_auth_cookie(cookies: dict[str, str]) -> bool:
    auth_fragments = ("session", "token", "auth", "passport", "odin_tt", "sid_guard", "uid_tt")
    return any(any(fragment in key.lower() for fragment in auth_fragments) for key in cookies)


def playwright_proxy_config(proxy_url: str) -> dict[str, str] | None:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return None
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    config = {"server": server}
    if parsed.username:
        config["username"] = unquote(parsed.username)
    if parsed.password:
        config["password"] = unquote(parsed.password)
    return config


def proxy_public_host(proxy_url: str) -> str:
    if not proxy_url:
        return ""
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return ""
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


def vid_from_download_url(url: str) -> str | None:
    match = re.search(r"/tos-mya[^/]*/([^/?]+)/", url)
    return match.group(1) if match else None


def mask_id(value: str | None) -> str | None:
    if not value:
        return None
    return f"*{value[-8:]}" if len(value) > 8 else f"*{value}"


def format_browser_diagnostic(diagnostic: dict[str, Any]) -> str:
    return (
        "Dola browser diagnostic: "
        f"cdp={diagnostic.get('cdp')}, "
        f"page={diagnostic.get('page_url')}, "
        f"endpoint={diagnostic.get('endpoint')}, "
        f"status={diagnostic.get('status_code')}, "
        f"error_code={diagnostic.get('error_code')}, "
        f"error_msg={diagnostic.get('error_msg')}, "
        f"conversation={diagnostic.get('conversation_id')}, "
        f"vid={diagnostic.get('vid')}, "
        f"download={diagnostic.get('has_download_url')}, "
        f"proxy={diagnostic.get('browser_proxy_active')}, "
        f"proxy_host={diagnostic.get('browser_proxy_host')}, "
        f"screenshot={diagnostic.get('screenshot_path')}, "
        f"body={diagnostic.get('body_snippet')}"
    )

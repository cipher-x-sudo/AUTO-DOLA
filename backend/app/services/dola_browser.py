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
from playwright.async_api import BrowserContext, Error as PlaywrightError, Locator, Page, async_playwright

from app.config import settings
from app.services.dola import (
    DolaSession,
    base_payload,
    build_video_prompt_text,
    cookie_names_from_header,
    build_unwatermarked_fallback_url,
    find_dola_fallback_apis,
    format_cookie_header,
    parse_conversation_from_stream,
    parse_dola_stream_error,
    parse_play_info,
    parse_unwatermarked_video_url,
    normalize_video_model,
    sanitize_dola_log_message,
)


DOLA_HOME_URL = "https://www.dola.com/"
DOLA_CREATE_IMAGE_URL = "https://www.dola.com/chat/create-image"
DOLA_CHAT_URL = "https://www.dola.com/chat/"
BROWSER_TIMEOUT_MS = 45_000
PROXY_PAGE_TIMEOUT_SECONDS = 120
DIRECT_PAGE_TIMEOUT_SECONDS = 45
AUTHENTICATED_PAGE_TIMEOUT_SECONDS = 90
VIDEO_MODE_TIMEOUT_SECONDS = 30
SUBMIT_READY_TIMEOUT_SECONDS = 30
READY_CARD_OPEN_TIMEOUT_SECONDS = 30
BROWSER_GENERATION_TIMEOUT_SECONDS = 900
BROWSER_SUBMIT_CAPTURE_TIMEOUT_SECONDS = 90
BROWSER_DEBUG_TOTAL_STEPS = 15
FRESH_BROWSER_COOKIE_NAMES = {
    # Browser/device identity. These must be minted by the fresh Chromium
    # context; importing them makes Dola repeatedly re-bootstrap the SPA.
    "ttwid",
    "s_v_web_id",
    "hook_slardar_session_id",
    "biz_trace_id",
    # Region/routing state is tied to the browser/IP that exported the cookie
    # file. Let Dola choose fresh routing for the current browser slot.
    "flow_user_country",
    "store-idc",
    "store-country-code",
    "store-country-code-src",
    # UI preferences and analytics are not authentication material.
    "i18next",
    "dbx-web-theme",
    "conversation_list_v2_group_mode",
    "_ga",
    "_ga_5mr93b9jt5",
    "_gcl_au",
}
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

def build_generation_patch_script(duration: int, model: str = "seedance_v2.0") -> str:
    target_duration = int(duration)
    target_model = normalize_video_model(model)
    return r"""
(() => {
    if (window.__autoDolaGenerationPatchInstalled) return;
    window.__autoDolaGenerationPatchInstalled = true;
    window.__autoDolaDurationPatchApplied = false;
    window.__autoDolaModelPatchApplied = false;
    const targetDuration = __AUTO_DOLA_DURATION__;
    const targetModel = __AUTO_DOLA_MODEL__;
    const originalStringify = JSON.stringify;
    JSON.stringify = function(value, replacer, space) {
        try {
            if (value && typeof value === 'object') {
                let str = originalStringify.apply(this, [value, replacer, space]);
                const isVideoGeneration = /seedance/i.test(str) || /\\?"ability_type\\?"\s*:\s*17/.test(str);
                if (!isVideoGeneration) return str;

                const durationFields = '(duration|video_duration|seconds|motion_seconds|video_length)';
                const plainDuration = new RegExp('"' + durationFields + '"\\s*:\\s*\\d+', 'g');
                const escapedDuration = new RegExp('\\\\"' + durationFields + '\\\\"\\s*:\\s*\\d+', 'g');
                const previousDuration = str;
                str = str.replace(plainDuration, (_match, key) => '"' + key + '":' + targetDuration);
                str = str.replace(escapedDuration, (_match, key) => '\\"' + key + '\\":' + targetDuration);
                if (str !== previousDuration) {
                    window.__autoDolaDurationPatchApplied = true;
                }

                const previousModel = str;
                str = str.replace(/"model"\s*:\s*"seedance[^"\\]*"/gi, '"model":"' + targetModel + '"');
                str = str.replace(/\\"model\\"\s*:\s*\\"seedance[^"\\]*\\"/gi, '\\"model\\":\\"' + targetModel + '\\"');
                if (str !== previousModel) window.__autoDolaModelPatchApplied = true;
                return str;
            }
        } catch (e) {
            console.error("AUTO-DOLA generation patch error:", e);
        }
        return originalStringify.apply(this, [value, replacer, space]);
    };
})();
""".replace("__AUTO_DOLA_DURATION__", str(target_duration)).replace("__AUTO_DOLA_MODEL__", json.dumps(target_model))


DURATION_PATCH_SCRIPT = build_generation_patch_script(15)


@dataclass
class BrowserNetworkState:
    conversation_id: str | None = None
    conversation_type: int = 3
    vid: str | None = None
    download_url: str | None = None
    unwatermarked_url: str | None = None
    seen_fallback_apis: set[str] = field(default_factory=set)
    fallback_resolutions_pending: int = 0
    error_code: int | None = None
    error_msg: str = ""
    last_endpoint: str = ""
    last_status: int | None = None
    last_body_snippet: str = ""
    captured_request: dict[str, Any] = field(default_factory=dict)
    captured_headers: dict[str, str] = field(default_factory=dict)
    captured_url: str = ""
    captured_method: str = ""
    requested_duration: int | None = None
    requested_model: str = ""
    requested_ratio: str = ""
    visible_duration: int | None = None
    visible_model: str = "seedance_v2.0"
    selected_model: str = ""
    selected_duration: int | None = None
    selected_ratio: str = ""
    captured_duration: int | None = None
    captured_model: str = ""
    captured_ratio: str = ""
    duration_patch_expected: bool = False
    duration_patch_applied: bool = False
    model_patch_expected: bool = False
    model_patch_applied: bool = False
    generation_options_verification: str = ""
    stage: str = "browser_start"
    last_successful_stage: str = "browser_started"
    stage_started_at: float = field(default_factory=time.monotonic)
    timeout_seconds: int | None = None
    visible_elements: list[str] = field(default_factory=list)
    images_blocked: bool = True
    resource_stats: dict[str, int] = field(default_factory=lambda: {"blocked_image_count": 0})
    video_tab_visible: bool = False
    model_visible: bool = False
    duration_visible: bool = False
    ratio_visible: bool = False
    textbox_visible: bool = False
    auth_cookie_count: int = 0
    main_frame_navigation_count: int = 0
    navigation_request_count: int = 0
    document_load_count: int = 0
    last_navigation_path: str = ""
    debug_enabled: bool = False
    debug_started_at: float = field(default_factory=time.monotonic)
    debug_step_index: int = 0
    debug_step_name: str = ""
    debug_steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DolaBrowserSubmitResult:
    session: DolaSession
    conversation_id: str
    conversation_type: int
    diagnostic: dict[str, Any]
    chat_url: str = ""
    submit_url: str = ""
    slot_id: str = ""


@dataclass
class DolaBrowserDownloadResult:
    download_url: str
    vid: str
    diagnostic: dict[str, Any]


class DolaBrowserError(RuntimeError):
    def __init__(self, message: str, diagnostic: dict[str, Any], error_type: str = "BROWSER_ERROR") -> None:
        super().__init__(message)
        self.error_type = error_type
        self.diagnostic = {
            **diagnostic,
            "error_type": error_type,
            "user_message": message,
        }


class DolaBrowserClient:
    def __init__(
        self,
        cdp_url: str = "",
        manual_url: str = "",
        screenshot_dir: Path | None = None,
        proxy_url: str = "",
        manager_url: str = "",
        headless: bool = False,
    ) -> None:
        self.cdp_url = cdp_url or settings.dola_browser_cdp_url
        self.manager_url = manager_url or settings.dola_browser_manager_url or self.cdp_url
        self.manual_url = manual_url or settings.dola_browser_manual_url
        self.screenshot_dir = screenshot_dir or (settings.log_dir / "dola-browser")
        self.proxy_url = proxy_url.strip()
        self.headless = headless
        self._active_slots: dict[str, dict[str, Any]] = {}
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> BrowserContext:
        slot = await self._launch_slot()
        runtime = await self._connect_slot(slot)
        self._active_slots[slot["slot_id"]] = runtime
        return runtime["context"]

    async def _launch_slot(self, profile_dir: str = "") -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{resolve_cdp_url(self.manager_url).rstrip('/')}/launch",
                json={"proxy_url": self.proxy_url, "profile_dir": profile_dir, "headless": self.headless},
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {"ok": False, "error": response.text}
            if response.is_error:
                error_type = str(payload.get("error") or "BROWSER_MANAGER_LAUNCH_FAILED")
                raise DolaBrowserError(
                    f"Dola browser launch failed: {error_type}",
                    {
                        "cdp": False,
                        "error_msg": payload.get("detail") or payload.get("error") or response.text,
                        "manager_status": response.status_code,
                        "slot_id": payload.get("slot_id"),
                        "profile_dir": payload.get("profile_dir"),
                        "log_file": payload.get("log_file"),
                        "body": payload.get("log_snippet") or "",
                    },
                    error_type,
                )
        if not payload.get("ok"):
            raise DolaBrowserError(f"Dola browser manager launch failed: {payload.get('error')}", {"cdp": False, "error_msg": payload.get("error")})
        return payload["slot"]

    async def _connect_slot(self, slot: dict[str, Any]) -> dict[str, Any]:
        playwright = await async_playwright().start()
        try:
            cdp_url = resolve_cdp_url(str(slot.get("container_cdp_url") or slot.get("cdp_url")))
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            runtime = {"slot": slot, "playwright": playwright, "browser": browser, "context": context}
            # The browser manager exposes a normal Chromium slot without proxy
            # credentials for most jobs.  CDP Fetch interception is unnecessary
            # in that case and can make modern Dola SPA navigations race/reload
            # while the page is still bootstrapping.  Keep interception for
            # authenticated proxies, where it is required for the challenge.
            handler_slot = {
                **slot,
                "disable_image_blocking": not bool(slot.get("proxy_username") and slot.get("proxy_password")),
            }
            await self._install_proxy_auth_handlers(context, handler_slot, runtime)
            return runtime
        except Exception:
            await playwright.stop()
            await self.close_slot(str(slot.get("slot_id") or ""))
            raise

    async def _install_proxy_auth_handlers(self, context: BrowserContext, slot: dict[str, Any], runtime: dict[str, Any]) -> None:
        username = str(slot.get("proxy_username") or "")
        password = str(slot.get("proxy_password") or "")
        has_proxy_auth = bool(username and password)
        sessions: list[Any] = []
        resource_stats = {"blocked_image_count": 0}

        if bool(slot.get("disable_image_blocking")) and not has_proxy_auth:
            runtime["proxy_auth_mode"] = str(slot.get("proxy_auth_mode") or "none")
            runtime["proxy_auth_sessions"] = sessions
            runtime["images_blocked"] = False
            runtime["resource_stats"] = resource_stats
            return

        async def install_for_page(page: Page) -> None:
            session = await context.new_cdp_session(page)
            sessions.append(session)

            async def on_auth_required(event: dict[str, Any]) -> None:
                await session.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": event["requestId"],
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": username,
                            "password": password,
                        },
                    },
                )

            async def on_request_paused(event: dict[str, Any]) -> None:
                if str(event.get("resourceType") or "").lower() == "image":
                    resource_stats["blocked_image_count"] += 1
                    await session.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
                    return
                await session.send("Fetch.continueRequest", {"requestId": event["requestId"]})

            if has_proxy_auth:
                session.on("Fetch.authRequired", lambda event: asyncio.create_task(on_auth_required(event)))
            session.on("Fetch.requestPaused", lambda event: asyncio.create_task(on_request_paused(event)))
            await session.send("Fetch.enable", {"handleAuthRequests": has_proxy_auth})

        for page in context.pages:
            await install_for_page(page)
        context.on("page", lambda page: asyncio.create_task(install_for_page(page)))
        runtime["proxy_auth_mode"] = "cdp" if has_proxy_auth else str(slot.get("proxy_auth_mode") or "none")
        runtime["proxy_auth_sessions"] = sessions
        runtime["images_blocked"] = True
        runtime["resource_stats"] = resource_stats

    async def close_slot(self, slot_id: str, *, delete_profile: bool = True) -> bool:
        runtime = self._active_slots.pop(slot_id, None)
        if runtime:
            try:
                await runtime["playwright"].stop()
            except Exception:
                pass
        if slot_id:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        f"{resolve_cdp_url(self.manager_url).rstrip('/')}/close",
                        json={"slot_id": slot_id, "delete_profile": delete_profile},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    return bool(payload.get("closed"))
            except Exception:
                return False
        return False

    async def delete_profile(self, profile_dir: str) -> bool:
        if not profile_dir:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{resolve_cdp_url(self.manager_url).rstrip('/')}/delete-profile",
                    json={"profile_dir": profile_dir},
                )
                response.raise_for_status()
                payload = response.json()
                return bool(payload.get("deleted"))
        except Exception:
            return False

    async def vpn_connect(self, *, config_path: str, config_name: str, username: str, password: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{resolve_cdp_url(self.manager_url).rstrip('/')}/vpn/connect",
                json={
                    "config_path": config_path,
                    "config_name": config_name,
                    "username": username,
                    "password": password,
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise DolaBrowserError(f"OpenVPN failed: {payload.get('error')}", {"cdp": False, "error_msg": payload.get("error")}, str(payload.get("error") or "VPN_CONNECT_FAILED"))
        return payload

    async def vpn_disconnect(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{resolve_cdp_url(self.manager_url).rstrip('/')}/vpn/disconnect", json={})
                response.raise_for_status()
                payload = response.json()
                return bool(payload.get("disconnected"))
        except Exception:
            return False

    async def launch_isolated_vpn_slot(self, *, config_path: str, config_name: str, username: str, password: str, log_fn: Any = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=180) as client:
            manager_url = resolve_cdp_url(self.manager_url).rstrip("/")
            launch_task = asyncio.create_task(client.post(
                f"{manager_url}/vpn-slot/launch",
                json={
                    "config_path": config_path,
                    "config_name": config_name,
                    "username": username,
                    "password": password,
                    "headless": self.headless,
                },
            ))
            started = time.monotonic()
            while not launch_task.done():
                done, _ = await asyncio.wait({launch_task}, timeout=5)
                if done:
                    break
                elapsed = int(time.monotonic() - started)
                stage = "container_starting"
                try:
                    status_response = await client.get(f"{manager_url}/status", timeout=3)
                    slots = status_response.json().get("vpn_slots") or []
                    matches = [slot for slot in slots if slot.get("config_name") == config_name]
                    if matches:
                        stage = str(max(matches, key=lambda slot: slot.get("started_at", 0)).get("stage") or stage)
                except Exception:
                    pass
                if log_fn:
                    log_fn(f"VPN slot {stage}: {elapsed}s/120s config={config_name}", "info")
            response = await launch_task
            try:
                payload = response.json()
            except ValueError:
                payload = {"ok": False, "error": response.text}
            if response.is_error:
                error_type = str(payload.get("error") or "VPN_SLOT_LAUNCH_FAILED")
                slot_id = str(payload.get("slot_id") or "")
                container_name = str(payload.get("container_name") or "")
                if slot_id or container_name:
                    await self.close_isolated_vpn_slot(slot_id=slot_id, container_name=container_name)
                detail = str(payload.get("detail") or payload.get("error") or response.text)
                concise_detail = next((line.strip() for line in reversed(detail.splitlines()) if line.strip()), error_type)
                raise DolaBrowserError(
                    f"VPN slot failed: {concise_detail}",
                    {
                        "cdp": False,
                        "error_msg": detail,
                        "manager_status": response.status_code,
                        "slot_id": slot_id,
                        "container_name": container_name,
                        "config_name": payload.get("config_name") or config_name,
                        "body": payload.get("log_snippet") or "",
                        "log_urls": payload.get("log_urls") or {},
                    },
                    error_type,
                )
        if not payload.get("ok"):
            raise DolaBrowserError(
                f"OpenVPN browser slot failed: {payload.get('error')}",
                {
                    "cdp": False,
                    "error_msg": payload.get("detail") or payload.get("error"),
                    "manager_status": response.status_code,
                    "slot_id": payload.get("slot_id"),
                    "container_name": payload.get("container_name"),
                    "config_name": payload.get("config_name") or config_name,
                    "body": payload.get("log_snippet") or "",
                },
                str(payload.get("error") or "VPN_SLOT_LAUNCH_FAILED"),
            )
        return payload

    async def update_isolated_vpn_slot_stage(self, slot_id: str, stage: str) -> bool:
        if not slot_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(
                    f"{resolve_cdp_url(self.manager_url).rstrip('/')}/vpn-slot/stage",
                    json={"slot_id": slot_id, "stage": stage},
                )
                response.raise_for_status()
                return bool(response.json().get("updated"))
        except Exception:
            return False

    async def close_isolated_vpn_slot(self, *, slot_id: str = "", container_name: str = "") -> bool:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{resolve_cdp_url(self.manager_url).rstrip('/')}/vpn-slot/close",
                    json={"slot_id": slot_id, "container_name": container_name},
                )
                response.raise_for_status()
                payload = response.json()
                return bool(payload.get("closed"))
        except Exception:
            return False

    async def kill_all_slots(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{resolve_cdp_url(self.manager_url).rstrip('/')}/kill-all", json={})
            response.raise_for_status()
            payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": False}

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
                vpn_response = await client.get(f"{manager_url}/vpn/status")
            manager_status = status_response.json()
            vpn_status = vpn_response.json() if vpn_response.status_code == 200 else {}
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
                "browser_vpn_active": bool(vpn_status.get("connected")),
                "browser_vpn_config": str(vpn_status.get("config_name") or ""),
                "browser_vpn_ip": str(vpn_status.get("ip") or ""),
                "browser_ip": browser_ip,
                "browser_headless": bool(manager_status.get("browser_headless", self.headless)),
                "page_count": manager_status.get("active_browser_count", 0),
                "active_browser_count": manager_status.get("active_browser_count", 0),
                "active_vpn_browser_count": manager_status.get("active_vpn_browser_count", 0),
                "vpn_slots": manager_status.get("vpn_slots", []),
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
                "browser_vpn_active": False,
                "browser_vpn_config": "",
                "browser_vpn_ip": "",
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

    async def submit_and_capture_session(
        self,
        prompt: str,
        duration: int,
        ratio: str,
        model: str = "seedance_v2.0",
        *,
        auth_cookies: list[dict[str, Any]] | None = None,
        log_fn: Any | None = None,
    ) -> DolaBrowserSubmitResult:
        slot: dict[str, Any] = {}
        slot_id = ""
        page: Page | None = None
        network = BrowserNetworkState(debug_enabled=True)
        self._debug_step(network, log_fn, 1, "Launch Chrome slot", "started")
        try:
            slot = await self._launch_slot()
            slot_id = str(slot["slot_id"])
            self._debug_step(network, log_fn, 1, "Launch Chrome slot", "completed", f"slot {slot.get('slot_number')}")
            self._debug_step(network, log_fn, 2, "Connect Chrome CDP", "started")
            runtime = await self._connect_slot(slot)
            self._active_slots[slot_id] = runtime
            network.images_blocked = bool(runtime.get("images_blocked"))
            network.resource_stats = runtime.get("resource_stats") or {"blocked_image_count": 0}
            self._debug_step(network, log_fn, 2, "Connect Chrome CDP", "completed")
            context = runtime["context"]
            self._debug_step(network, log_fn, 3, "Inject encrypted cookie profile", "started")
            if auth_cookies:
                browser_cookies, fresh_cookie_count = self._browser_auth_cookies(auth_cookies)
                network.auth_cookie_count = len(browser_cookies)
                await context.add_cookies(browser_cookies)
                self._debug_step(
                    network,
                    log_fn,
                    3,
                    "Inject encrypted cookie profile",
                    "completed",
                    f"{len(browser_cookies)} auth cookies injected; {fresh_cookie_count} browser-state cookies skipped for fresh values",
                )
            else:
                self._debug_step(network, log_fn, 3, "Inject encrypted cookie profile", "completed", "anonymous mode")
            initial_page_count = len(context.pages)
            self._debug_step(network, log_fn, 4, "Navigate to Dola AI Creation", "started")
            if log_fn:
                log_fn("Navigating to Dola AI Creation route", "info")
            page = await self.new_patched_job_page(context, int(duration), network, model=model, log_fn=log_fn)
            if log_fn:
                log_fn("Dola navigation completed; waiting for page controls", "success")
            self._debug_step(network, log_fn, 4, "Navigate to Dola AI Creation", "completed", page.url)
            final_page_count = len(context.pages)
            closed_blank_pages = max(0, initial_page_count - final_page_count)
            if log_fn:
                if runtime.get("proxy_auth_mode") == "cdp":
                    log_fn("Proxy auth handled through CDP", "info")
                log_fn(f"Closed extra blank tabs: {closed_blank_pages}", "info")
            page.on("request", lambda request: self._capture_request(request, network))
            page.on("response", lambda response: asyncio.create_task(self._capture_response(response, network)))
            self._debug_step(network, log_fn, 5, "Wait for Dola page readiness", "started")
            await self._ensure_dola_ready(page, network, log_fn)
            self._debug_step(network, log_fn, 5, "Wait for Dola page readiness", "completed")
            self._debug_step(network, log_fn, 6, "Activate Video mode", "started")
            await self._select_video_mode(page, network, log_fn)
            self._debug_step(network, log_fn, 6, "Activate Video mode", "completed")
            await self._select_generation_options(page, int(duration), ratio, network, log_fn)
            full_prompt = build_browser_video_prompt_text(prompt, int(duration), ratio)
            await self._submit_via_ui(page, full_prompt, network, log_fn)
            self._debug_step(network, log_fn, 13, "Capture chat/completion request", "started")
            result = await self._wait_for_submit_capture(context, page, network, log_fn)
            result.slot_id = slot_id
            result.diagnostic["slot_id"] = slot_id
            result.diagnostic["slot_number"] = slot.get("slot_number")
            result.diagnostic["profile_dir"] = slot.get("profile_dir")
            result.diagnostic["cdp_port"] = slot.get("external_port") or slot.get("port")
            result.diagnostic["initial_page_count"] = initial_page_count
            result.diagnostic["final_page_count"] = final_page_count
            result.diagnostic["closed_blank_pages"] = closed_blank_pages
            if log_fn:
                log_fn("Captured browser session", "success")
            return result
        except DolaBrowserError as exc:
            self._fail_current_debug_step(network, log_fn, str(exc))
            exc.diagnostic.update(
                {
                    "browser_debug_step": network.debug_step_name,
                    "browser_debug_step_index": network.debug_step_index,
                    "browser_debug_steps": list(network.debug_steps),
                }
            )
            await self._close_failed_submit_slot(slot_id, log_fn)
            raise
        except Exception as exc:
            self._fail_current_debug_step(network, log_fn, str(exc))
            screenshot_path = await self._screenshot(page, "browser-failure") if page else ""
            await self._close_failed_submit_slot(slot_id, log_fn)
            diagnostic = self._diagnostic(page, network, screenshot_path=screenshot_path) if page else {
                "cdp": False,
                "page_url": "",
                "manual_url": self.manual_url,
                "endpoint": network.last_endpoint,
                "status_code": network.last_status,
                "error_code": network.error_code,
                "error_msg": network.error_msg,
                "body_snippet": network.last_body_snippet,
                "conversation_id": mask_id(network.conversation_id),
                "vid": mask_id(network.vid),
                "has_download_url": bool(network.download_url),
                "captured_request": network.captured_request,
                "captured_endpoint": endpoint_name(network.captured_url),
                "browser_debug_step": network.debug_step_name,
                "browser_debug_step_index": network.debug_step_index,
                "browser_debug_steps": list(network.debug_steps),
                "cookie_names": [],
                "screenshot_path": screenshot_path,
                "browser_proxy_active": bool(self.proxy_url),
                "browser_proxy_host": proxy_public_host(self.proxy_url),
            }
            raise DolaBrowserError(
                f"Dola browser failed: {exc}",
                diagnostic,
            ) from exc

    async def _close_failed_submit_slot(self, slot_id: str, log_fn: Any | None = None) -> None:
        if not slot_id:
            return
        if log_fn:
            log_fn("Deleting browser profile after rejection", "info")
        closed = await self.close_slot(slot_id)
        if log_fn:
            log_fn("Browser profile cleaned" if closed else f"Browser cleanup failed: {slot_id}", "success" if closed else "error")

    async def reopen_profile_and_wait_for_ready_download(
        self,
        *,
        profile_dir: str,
        chat_url: str,
        conversation_id: str,
        log_fn: Any | None = None,
        timeout_seconds: int = 240,
    ) -> DolaBrowserDownloadResult:
        slot = await self._launch_slot(profile_dir=profile_dir)
        slot_id = str(slot["slot_id"])
        if log_fn:
            log_fn(f"Reopening saved browser profile in slot {slot.get('slot_number')}", "info")
        try:
            runtime = await self._connect_slot(slot)
            self._active_slots[slot_id] = runtime
            context = runtime["context"]
            page = await self._allocate_job_page(context)
            await page.goto(chat_url or f"{DOLA_CHAT_URL}{conversation_id}", wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            if log_fn:
                log_fn("Saved browser profile reopened", "success")
            return await self.wait_for_ready_video_download(
                conversation_id,
                slot_id=slot_id,
                log_fn=log_fn,
                timeout_seconds=timeout_seconds,
                poll_attempt_label="browser fallback",
            )
        finally:
            await self.close_slot(slot_id, delete_profile=True)

    async def wait_for_download_from_ready_card(
        self,
        conversation_id: str,
        *,
        slot_id: str = "",
        log_fn: Any | None = None,
        timeout_seconds: int = 240,
    ) -> DolaBrowserDownloadResult:
        return await self.wait_for_ready_video_download(
            conversation_id,
            slot_id=slot_id,
            log_fn=log_fn,
            timeout_seconds=timeout_seconds,
        )

    async def wait_for_ready_video_download(
        self,
        conversation_id: str,
        *,
        slot_id: str = "",
        log_fn: Any | None = None,
        timeout_seconds: int = 240,
        poll_attempt_label: str = "",
    ) -> DolaBrowserDownloadResult:
        page = await self._slot_page(slot_id, conversation_id) if slot_id else await self._page_for_conversation(await self.connect(), conversation_id)
        network = BrowserNetworkState(conversation_id=conversation_id)
        runtime = self._active_slots.get(slot_id) if slot_id else None
        if runtime:
            network.images_blocked = bool(runtime.get("images_blocked"))
            network.resource_stats = runtime.get("resource_stats") or {"blocked_image_count": 0}
        self._set_stage(network, "waiting_for_video_ready", timeout_seconds)
        page.on("response", lambda response: asyncio.create_task(self._capture_response(response, network)))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if network.error_code:
                raise DolaBrowserError(self._message_for_error(network), self._diagnostic(page, network))
            await self._dismiss_login_popup(page, network, log_fn)
            await self._dismiss_modal_overlays(page)
            if network.unwatermarked_url:
                if log_fn:
                    log_fn("Browser captured raw unwatermarked Dola source.", "success")
                return DolaBrowserDownloadResult(
                    download_url=network.unwatermarked_url,
                    vid=network.vid or vid_from_download_url(network.unwatermarked_url) or f"browser-{conversation_id[-8:]}",
                    diagnostic=self._diagnostic(page, network),
                )
            direct_src = await self._video_src_from_dom(page)
            if direct_src:
                if network.fallback_resolutions_pending:
                    await asyncio.sleep(0.25)
                    continue
                if log_fn:
                    log_fn("Browser says video ready.", "success")
                return DolaBrowserDownloadResult(
                    download_url=direct_src,
                    vid=vid_from_download_url(direct_src) or f"browser-{conversation_id[-8:]}",
                    diagnostic=self._diagnostic(page, network),
                )
            if await self._click_ready_video_card(page):
                if log_fn:
                    log_fn("Browser says video ready.", "success")
                    log_fn("Opened ready Dola video card to capture play_info.", "info")
                    log_fn("Capturing play_info from browser ready card.", "info")
                self._set_stage(network, "capturing_play_info", READY_CARD_OPEN_TIMEOUT_SECONDS)
                capture_deadline = time.monotonic() + READY_CARD_OPEN_TIMEOUT_SECONDS
                while time.monotonic() < capture_deadline:
                    if network.unwatermarked_url:
                        if log_fn:
                            log_fn("Captured fallback_api raw unwatermarked source.", "success")
                        return DolaBrowserDownloadResult(
                            download_url=network.unwatermarked_url,
                            vid=network.vid or vid_from_download_url(network.unwatermarked_url) or f"browser-{conversation_id[-8:]}",
                            diagnostic=self._diagnostic(page, network),
                        )
                    direct_src = await self._video_src_from_dom(page)
                    if direct_src:
                        if network.fallback_resolutions_pending:
                            await asyncio.sleep(0.25)
                            continue
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
                    await asyncio.sleep(0.5)
                screenshot_path = await self._screenshot(page, "play-info-not-captured")
                raise DolaBrowserError(
                    "Dola showed the completed video, but its download URL was not captured.",
                    self._diagnostic(page, network, screenshot_path=screenshot_path),
                    "PLAY_INFO_NOT_CAPTURED",
                )
            if log_fn and poll_attempt_label:
                log_fn(f"Waiting for browser ready card during {poll_attempt_label}.", "debug")
            await asyncio.sleep(3)
        screenshot_path = await self._screenshot(page, "download-not-found")
        ready_text = page.get_by_text("Your video is ready.", exact=False)
        ready_visible = False
        for index in range(await ready_text.count()):
            if await ready_text.nth(index).is_visible():
                ready_visible = True
                break
        if ready_visible and "ready_text" not in network.visible_elements:
            network.visible_elements.append("ready_text")
        error_type = "READY_CARD_NOT_CLICKABLE" if ready_visible else "PLAY_INFO_NOT_CAPTURED"
        message = (
            "Dola showed the completed video, but its video card could not be opened."
            if error_type == "READY_CARD_NOT_CLICKABLE"
            else "Dola did not expose a completed video or download URL before the timeout."
        )
        raise DolaBrowserError(message, self._diagnostic(page, network, screenshot_path=screenshot_path), error_type)

    async def _page(self, context: BrowserContext) -> Page:
        for page in context.pages:
            if "dola.com" in page.url:
                return page
        page = await context.new_page()
        await page.goto(DOLA_HOME_URL, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        return page

    async def new_job_page(self, context: BrowserContext | None = None) -> Page:
        context = context or await self.connect()
        page = await self._allocate_job_page(context)
        timeout_ms = (PROXY_PAGE_TIMEOUT_SECONDS if self.proxy_url else DIRECT_PAGE_TIMEOUT_SECONDS) * 1000
        await page.goto(DOLA_CREATE_IMAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        return page

    async def _allocate_job_page(self, context: BrowserContext) -> Page:
        blank_pages: list[Page] = []
        for page in list(context.pages):
            page_url = str(page.url or "")
            if page_url in {"", "about:blank"} or page_url.startswith("chrome://new-tab-page"):
                blank_pages.append(page)
        if blank_pages:
            page = blank_pages[0]
            for extra_page in blank_pages[1:]:
                try:
                    await extra_page.close()
                except Exception:
                    pass
            return page
        return await context.new_page()

    async def new_patched_job_page(
        self,
        context: BrowserContext,
        duration: int,
        network: BrowserNetworkState,
        *,
        model: str = "seedance_v2.0",
        log_fn: Any | None = None,
    ) -> Page:
        model = normalize_video_model(model)
        network.requested_duration = int(duration)
        network.requested_model = model
        network.visible_duration = browser_visible_duration(duration)
        network.visible_model = "seedance_v2.0"
        network.duration_patch_expected = int(duration) not in {5, 10}
        network.model_patch_expected = model != "seedance_v2.0"
        if network.duration_patch_expected or network.model_patch_expected:
            await context.add_init_script(build_generation_patch_script(duration, model))
        page = await self._allocate_job_page(context)
        self._install_navigation_debug(page, network, log_fn)
        timeout_seconds = PROXY_PAGE_TIMEOUT_SECONDS if self.proxy_url else DIRECT_PAGE_TIMEOUT_SECONDS
        self._set_stage(network, "page_navigation", timeout_seconds)
        # Keep this message generic: it is useful for diagnosing navigation
        # stalls without logging cookies, query values, or prompt text.
        if network.debug_enabled:
            network.debug_step_name = "Navigate to Dola AI Creation"
        try:
            await page.goto(DOLA_CREATE_IMAGE_URL, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        except PlaywrightError as exc:
            screenshot_path = await self._screenshot(page, "page-navigation-timeout")
            message = (
                f"Dola page did not finish loading through proxy after {timeout_seconds} seconds."
                if self.proxy_url
                else f"Dola page did not finish loading after {timeout_seconds} seconds."
            )
            raise DolaBrowserError(message, self._diagnostic(page, network, screenshot_path=screenshot_path), "PAGE_LOAD_TIMEOUT") from exc
        network.last_successful_stage = "page_navigated"
        return page

    def _install_navigation_debug(self, page: Page, network: BrowserNetworkState, log_fn: Any | None) -> None:
        def log(message: str, level: str = "info") -> None:
            if log_fn and network.debug_enabled:
                log_fn(message, level)

        def on_frame_navigated(frame: Any) -> None:
            try:
                if frame != page.main_frame:
                    return
                network.main_frame_navigation_count += 1
                network.last_navigation_path = urlparse(str(frame.url or "")).path or "/"
                count = network.main_frame_navigation_count
                if count in {1, 2, 5, 10} or count % 25 == 0:
                    level = "warn" if count >= 5 else "debug"
                    log(f"Browser navigation event #{count}; path={network.last_navigation_path}", level)
            except Exception:
                return

        def on_request(request: Any) -> None:
            try:
                if not request.is_navigation_request() or request.frame != page.main_frame:
                    return
                network.navigation_request_count += 1
                path = urlparse(str(request.url or "")).path or "/"
                level = "warn" if network.navigation_request_count > 1 else "info"
                log(f"Browser document request #{network.navigation_request_count}; path={path}", level)
            except Exception:
                return

        def on_dom_content_loaded() -> None:
            network.document_load_count += 1
            level = "warn" if network.document_load_count > 1 else "success"
            log(f"Browser DOMContentLoaded #{network.document_load_count}", level)

        page.on("framenavigated", on_frame_navigated)
        page.on("request", on_request)
        page.on("domcontentloaded", on_dom_content_loaded)

    async def _page_for_conversation(self, context: BrowserContext, conversation_id: str) -> Page:
        for page in context.pages:
            if conversation_id in page.url:
                return page
        page = await self._page(context)
        await page.goto(f"{DOLA_CHAT_URL}{conversation_id}", wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        return page

    async def _ensure_dola_ready(self, page: Page, network: BrowserNetworkState, log_fn: Any | None = None) -> None:
        timeout_seconds = PROXY_PAGE_TIMEOUT_SECONDS if self.proxy_url else DIRECT_PAGE_TIMEOUT_SECONDS
        if network.auth_cookie_count:
            timeout_seconds = max(timeout_seconds, AUTHENTICATED_PAGE_TIMEOUT_SECONDS)
        if DOLA_CREATE_IMAGE_URL not in page.url:
            if log_fn:
                log_fn("Navigating to Dola AI Creation route", "info")
            await page.goto(DOLA_CREATE_IMAGE_URL, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        if log_fn and network.debug_enabled:
            log_fn("Dola document loaded; checking AI Creation controls", "info")
        self._set_stage(network, "page_loading", timeout_seconds)
        # Authenticated Dola accounts render the sidebar first and can take
        # considerably longer to hydrate the AI Creation panel. Reloading the
        # half-mounted route restarts that work and leaves the panel blank, so
        # wait on the same document and give cookie-backed sessions more time.
        started = time.monotonic()
        last_logged = -5
        while time.monotonic() - started < timeout_seconds:
            await self._dismiss_cookie_banner(page)
            await self._dismiss_login_popup(page, network, log_fn)
            await self._raise_if_blocked(page, network)
            network.visible_elements = await self._visible_dola_elements(page)
            video_tab = await self._first_visible(page.get_by_role("tab", name="Video", exact=True))
            if video_tab is not None:
                if log_fn and network.debug_enabled:
                    log_fn("Video tab control detected; page hydration complete", "success")
                network.last_successful_stage = "page_ready"
                return
            elapsed = int(time.monotonic() - started)
            if log_fn and elapsed >= last_logged + 5:
                visible = ",".join(network.visible_elements) if network.visible_elements else "none"
                log_fn(f"Waiting for Dola page {elapsed}/{timeout_seconds}s; visible={visible}", "info")
                last_logged = elapsed
            await asyncio.sleep(1)
        screenshot_path = await self._screenshot(page, "page-load-timeout")
        message = (
            f"Dola page did not finish loading through proxy after {timeout_seconds} seconds."
            if self.proxy_url
            else f"Dola page did not finish loading after {timeout_seconds} seconds."
        )
        raise DolaBrowserError(
            message,
            self._diagnostic(page, network, screenshot_path=screenshot_path),
            "PAGE_LOAD_TIMEOUT",
        )

    async def _raise_if_blocked(self, page: Page, network: BrowserNetworkState) -> None:
        body_text = sanitize_dola_log_message((await page.locator("body").inner_text(timeout=10_000))[:2000])
        lowered = body_text.lower()
        if "requires a username and password" in lowered or "err_proxy" in lowered or "proxy authentication" in lowered:
            screenshot_path = await self._screenshot(page, "proxy-auth-failed")
            raise DolaBrowserError(
                "The browser proxy rejected its username or password.",
                self._diagnostic(page, network, screenshot_path=screenshot_path, body_snippet=body_text),
                "PROXY_AUTH_FAILED",
            )
        if "not available in your country" in lowered or "country/region" in lowered:
            raise DolaBrowserError("Dola is not available in the proxy region.", self._diagnostic(page, network, body_snippet=body_text), "COUNTRY_RESTRICTED")
        if "captcha" in lowered or "verify you are human" in lowered or "security verification" in lowered:
            screenshot_path = await self._screenshot(page, "captcha-block")
            raise DolaBrowserError("Dola blocked the browser with a CAPTCHA.", self._diagnostic(page, network, screenshot_path=screenshot_path, body_snippet=body_text), "CAPTCHA_BLOCK")
        if "login required to continue" in lowered or "you must log in to continue" in lowered:
            screenshot_path = await self._screenshot(page, "login-required")
            raise DolaBrowserError("Dola requires authentication to continue generation.", self._diagnostic(page, network, screenshot_path=screenshot_path, body_snippet=body_text), "LOGIN_REQUIRED")

    async def _submit_via_ui(self, page: Page, prompt: str, network: BrowserNetworkState, log_fn: Any | None = None) -> None:
        await self._dismiss_cookie_banner(page)
        await self._dismiss_login_popup(page, network, log_fn)
        await self._dismiss_modal_overlays(page)
        self._set_stage(network, "waiting_for_textbox", SUBMIT_READY_TIMEOUT_SECONDS)
        if log_fn and network.debug_enabled:
            log_fn("Finding video prompt textbox", "info")
        input_locator = await self._wait_for_video_textbox(page, SUBMIT_READY_TIMEOUT_SECONDS)
        if input_locator is None:
            screenshot_path = await self._screenshot(page, "input-not-found")
            network.visible_elements = await self._visible_dola_elements(page)
            raise DolaBrowserError(
                "Dola loaded, but the video prompt textbox did not appear.",
                self._diagnostic(page, network, screenshot_path=screenshot_path),
                "TEXTBOX_NOT_FOUND",
            )

        submit_button = await self._find_submit_button_candidate(page, input_locator)
        self._debug_step(network, log_fn, 10, "Enter video prompt", "started")
        if log_fn:
            if network.debug_enabled:
                log_fn("Clicking video prompt textbox", "info")
                log_fn("Typing prompt into video textbox", "info")
            log_fn("Entering prompt", "info")
        await input_locator.click(timeout=10_000)
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(prompt)
        except PlaywrightError:
            await page.keyboard.type(prompt, delay=1)
        entered_text = (await input_locator.inner_text()).strip()
        if prompt not in entered_text:
            screenshot_path = await self._screenshot(page, "prompt-entry-failed")
            raise DolaBrowserError(
                "Dola did not accept the prompt text in its video textbox.",
                self._diagnostic(page, network, screenshot_path=screenshot_path),
                "TEXTBOX_NOT_FOUND",
            )
        if log_fn:
            if network.debug_enabled:
                log_fn("Prompt text verified in video textbox", "success")
            log_fn("Prompt verified", "success")
        self._debug_step(network, log_fn, 10, "Enter video prompt", "completed", f"{len(prompt)} characters")
        self._set_stage(network, "waiting_for_submit_button", SUBMIT_READY_TIMEOUT_SECONDS)
        self._debug_step(network, log_fn, 11, "Wait for enabled submit button", "started")
        if log_fn:
            if network.debug_enabled:
                log_fn("Finding enabled submit button", "info")
            log_fn("Waiting for submit button", "info")
        button = await self._wait_for_submit_button(page, input_locator, SUBMIT_READY_TIMEOUT_SECONDS, submit_button)
        if button is None:
            screenshot_path = await self._screenshot(page, "submit-disabled")
            network.visible_elements = await self._visible_dola_elements(page)
            raise DolaBrowserError(
                "Dola kept the video submit button disabled after the prompt was entered.",
                self._diagnostic(page, network, screenshot_path=screenshot_path),
                "SUBMIT_BUTTON_DISABLED",
            )
        self._debug_step(network, log_fn, 11, "Wait for enabled submit button", "completed", "button enabled")
        self._debug_step(network, log_fn, 12, "Click submit", "started")
        if log_fn:
            if network.debug_enabled:
                log_fn("Clicking enabled submit button", "info")
            log_fn("Submitting prompt", "info")
        await button.click(timeout=10_000)
        self._debug_step(network, log_fn, 12, "Click submit", "completed")
        network.last_successful_stage = "prompt_submitted"
        self._set_stage(network, "capturing_submission", BROWSER_SUBMIT_CAPTURE_TIMEOUT_SECONDS)

    async def _select_video_mode(self, page: Page, network: BrowserNetworkState, log_fn: Any | None = None) -> None:
        self._set_stage(network, "selecting_video_mode", VIDEO_MODE_TIMEOUT_SECONDS)
        if log_fn and network.debug_enabled:
            log_fn("Finding Video tab control", "info")
        video_tabs = page.get_by_role("tab", name="Video", exact=True)
        video_tab = await self._first_visible(video_tabs)
        if video_tab is None:
            screenshot_path = await self._screenshot(page, "video-tab-missing")
            raise DolaBrowserError("Dola loaded without its Video tab.", self._diagnostic(page, network, screenshot_path=screenshot_path), "VIDEO_MODE_NOT_READY")
        try:
            if log_fn and network.debug_enabled:
                log_fn("Clicking Video tab", "info")
            await self._click_control(video_tab)
        except PlaywrightError:
            # Authenticated Dola routes can replace the segmented control on
            # the first hydration tick. Reacquire the tab before retrying so a
            # detached locator cannot abort an otherwise healthy session.
            video_tab = await self._wait_for_first_visible(video_tabs, 3)
            if video_tab is None:
                raise
            await self._click_control(video_tab)
        network.video_tab_visible = True
        if log_fn:
            if network.debug_enabled:
                log_fn("Video tab clicked; waiting for video controls", "info")
            log_fn("Video tab clicked", "info")
        deadline = time.monotonic() + VIDEO_MODE_TIMEOUT_SECONDS
        last_video_click = time.monotonic()
        last_control_state = ""
        while time.monotonic() < deadline:
            await self._dismiss_login_popup(page, network, log_fn)
            await self._raise_if_blocked(page, network)
            video_tab = await self._first_visible(video_tabs)
            if video_tab is None:
                network.video_tab_visible = False
                await asyncio.sleep(0.5)
                continue
            model = await self._visible_model_button(page)
            ratio = await self._first_visible(page.get_by_role("button", name="Ratio", exact=True))
            textbox = await self._find_video_textbox(page)
            duration_visible = await self._duration_control_visible(page)
            try:
                network.video_tab_visible = await video_tab.is_visible()
            except PlaywrightError:
                # React may swap the tab node while the model controls mount.
                # The next iteration reacquires it from the role locator.
                network.video_tab_visible = False
                await asyncio.sleep(0.5)
                continue
            network.model_visible = model is not None
            network.duration_visible = duration_visible
            network.ratio_visible = ratio is not None
            network.textbox_visible = textbox is not None
            if log_fn and network.debug_enabled:
                control_state = (
                    f"tab={'yes' if network.video_tab_visible else 'no'}, "
                    f"model={'yes' if network.model_visible else 'no'}, "
                    f"ratio={'yes' if network.ratio_visible else 'no'}, "
                    f"duration={'yes' if network.duration_visible else 'no'}, "
                    f"textbox={'yes' if network.textbox_visible else 'no'}"
                )
                if control_state != last_control_state:
                    log_fn(f"Checking Video controls: {control_state}", "debug")
                    last_control_state = control_state
            video_active = await self._video_tab_active(video_tab, model, duration_visible)
            if video_active and network.model_visible and network.duration_visible and network.ratio_visible and network.textbox_visible:
                network.last_successful_stage = "video_mode_ready"
                if log_fn:
                    if network.debug_enabled:
                        log_fn("Video mode active: model, ratio, duration, and textbox controls ready", "success")
                    log_fn("Video controls ready", "success")
                return
            elapsed = time.monotonic() - last_video_click
            if not video_active and elapsed >= 2:
                fresh_video_tab = await self._first_visible(video_tabs)
                if fresh_video_tab is not None:
                    try:
                        if log_fn and network.debug_enabled:
                            log_fn("Video tab inactive; reacquiring and clicking Video tab", "debug")
                        await self._click_control(fresh_video_tab)
                        last_video_click = time.monotonic()
                        if log_fn:
                            log_fn("Video tab still inactive; retrying the tab click", "debug")
                    except PlaywrightError:
                        # A concurrent hydration replacement is transient; the
                        # next loop will reacquire and try again.
                        last_video_click = time.monotonic()
            await asyncio.sleep(0.5)
        screenshot_path = await self._screenshot(page, "video-mode-not-ready")
        network.visible_elements = await self._visible_dola_elements(page)
        raise DolaBrowserError(
            "Dola opened, but its video controls did not finish loading.",
            self._diagnostic(page, network, screenshot_path=screenshot_path),
            "VIDEO_MODE_NOT_READY",
        )

    async def _select_generation_options(
        self,
        page: Page,
        duration: int,
        ratio: str,
        network: BrowserNetworkState,
        log_fn: Any | None = None,
    ) -> None:
        if ratio not in {"9:16", "16:9", "1:1"}:
            raise DolaBrowserError(f"Unsupported Dola video ratio: {ratio}.", self._diagnostic(page, network), "GENERATION_OPTIONS_MISMATCH")
        if duration not in {5, 10, 15, 30, 60}:
            raise DolaBrowserError(f"Unsupported Dola video duration: {duration}.", self._diagnostic(page, network), "GENERATION_OPTIONS_MISMATCH")
        network.requested_duration = duration
        network.requested_ratio = ratio
        self._set_stage(network, "selecting_generation_options", VIDEO_MODE_TIMEOUT_SECONDS)
        if network.requested_model:
            self._debug_step(network, log_fn, 7, "Select Seedance model", "started", network.requested_model)
            await self._select_model(page, network.requested_model, network, log_fn)
            self._debug_step(network, log_fn, 7, "Select Seedance model", "completed", network.selected_model)
        self._debug_step(network, log_fn, 8, "Select aspect ratio", "started", ratio)
        await self._select_ratio(page, ratio, network, log_fn)
        self._debug_step(network, log_fn, 8, "Select aspect ratio", "completed", network.selected_ratio)
        self._debug_step(network, log_fn, 9, "Select visible duration control", "started", f"requested {duration}s")
        await self._select_duration(page, browser_visible_duration(duration), network, log_fn)
        self._debug_step(network, log_fn, 9, "Select visible duration control", "completed", f"UI {network.selected_duration}s; request patch {duration}s")
        network.last_successful_stage = "generation_options_selected"

    async def _select_model(self, page: Page, model: str, network: BrowserNetworkState, log_fn: Any | None) -> None:
        model = normalize_video_model(model)
        await self._dismiss_login_popup(page, network, log_fn)
        target_label = "Dreamina Seedance 2.5" if model == "seedance_v2.5" else "Dreamina Seedance 2.0 Fast"
        short_label = "2.5" if model == "seedance_v2.5" else "2.0 Fast"
        if log_fn:
            if network.debug_enabled:
                log_fn(f"Waiting for model control ({short_label})", "info")
            log_fn(f"Selecting model {short_label}", "info")
        trigger = await self._wait_for_model_button(page, 10)
        if trigger is None:
            await self._raise_options_error(page, network, f"Dola model control was not found for {short_label}.")
        current_name = " ".join((await trigger.inner_text()).split())
        if log_fn and network.debug_enabled:
            log_fn(f"Model control found (current: {current_name or 'unknown'}); target: {short_label}", "info")
        if short_label.lower() not in current_name.lower():
            if log_fn and network.debug_enabled:
                log_fn("Opening model menu", "info")
            await self._click_control(trigger)
            menu = await self._wait_for_option_menu(page, 10)
            if menu is None:
                await self._raise_options_error(page, network, "Dola model menu did not open.")
            option = await self._first_visible(menu.get_by_role("menuitem", name=re.compile(re.escape(target_label), re.I)))
            if option is None:
                await self._raise_options_error(page, network, f"Dola model option {short_label} was not available.")
            if log_fn and network.debug_enabled:
                log_fn(f"Clicking model option {short_label}", "info")
            await self._click_control(option)
        selected = await self._wait_for_first_visible(
            page.get_by_role("button", name=re.compile(rf"^Model\s+{re.escape(short_label)}$", re.I)),
            10,
        )
        if selected is None:
            await self._raise_options_error(page, network, f"Dola did not apply model {short_label}.")
        network.selected_model = model
        network.visible_model = model
        if log_fn:
            log_fn(f"Model selected: {short_label}", "success")

    async def _select_ratio(self, page: Page, ratio: str, network: BrowserNetworkState, log_fn: Any | None) -> None:
        await self._dismiss_login_popup(page, network, log_fn)
        if log_fn:
            if network.debug_enabled:
                log_fn(f"Finding ratio control (target: {ratio})", "info")
            log_fn(f"Selecting ratio {ratio}", "info")
        ratio_button_names = (
            "Ratio",
            "Ratio 9:16",
            "Ratio 16:9",
            "Ratio 1:1",
            "Ratio 3:4",
            "Ratio 4:3",
            "Ratio 21:9",
        )
        trigger = await self._visible_named_button(page, ratio_button_names)
        if trigger is None:
            await self._raise_options_error(page, network, f"Dola ratio control was not found for {ratio}.")
        current_name = " ".join((await trigger.inner_text()).split())
        if current_name not in {ratio, f"Ratio {ratio}"}:
            if log_fn and network.debug_enabled:
                log_fn("Opening ratio menu", "info")
            await self._click_control(trigger)
            menu = await self._wait_for_option_menu(page, 10)
            if menu is None:
                await self._raise_options_error(page, network, "Dola ratio menu did not open.")
            option = await self._first_visible(menu.get_by_role("menuitem", name=ratio, exact=True))
            if option is None:
                await self._raise_options_error(page, network, f"Dola ratio option {ratio} was not available.")
            if log_fn and network.debug_enabled:
                log_fn(f"Clicking ratio option {ratio}", "info")
            await self._click_control(option)
        selected = await self._wait_for_first_visible(
            page.get_by_role("button", name=f"Ratio {ratio}", exact=True),
            10,
        )
        if selected is None:
            selected = await self._wait_for_first_visible(
                page.get_by_role("button", name=ratio, exact=True),
                1,
            )
        if selected is None:
            await self._raise_options_error(page, network, f"Dola did not apply ratio {ratio}.")
        network.selected_ratio = ratio
        if log_fn:
            log_fn(f"Ratio selected: {ratio}", "success")

    async def _select_duration(self, page: Page, duration: int, network: BrowserNetworkState, log_fn: Any | None) -> None:
        await self._dismiss_login_popup(page, network, log_fn)
        target = f"{duration}s"
        if log_fn:
            if network.debug_enabled:
                log_fn(f"Finding duration control (target: {target})", "info")
            log_fn(f"Selecting duration {target}", "info")
        trigger = await self._visible_named_button(page, ("5s", "10s"))
        if trigger is None:
            await self._raise_options_error(page, network, f"Dola duration control was not found for {target}.")
        current_name = (await trigger.inner_text()).strip()
        if current_name != target:
            if log_fn and network.debug_enabled:
                log_fn("Opening duration menu", "info")
            await self._click_control(trigger)
            menu = await self._wait_for_option_menu(page, 10)
            if menu is None:
                await self._raise_options_error(page, network, "Dola duration menu did not open.")
            option = await self._first_visible(menu.get_by_role("menuitem", name=target, exact=True))
            if option is None:
                await self._raise_options_error(page, network, f"Dola duration option {target} was not available.")
            if log_fn and network.debug_enabled:
                log_fn(f"Clicking duration option {target}", "info")
            await self._click_control(option)
        selected = await self._wait_for_first_visible(page.get_by_role("button", name=target, exact=True), 10)
        if selected is None:
            await self._raise_options_error(page, network, f"Dola did not apply duration {target}.")
        network.selected_duration = duration
        if log_fn:
            log_fn(f"Duration selected: {target}", "success")

    async def _visible_named_button(self, page: Page, names: tuple[str, ...]) -> Locator | None:
        for name in names:
            locator = page.get_by_role("button", name=name, exact=True)
            visible = await self._first_visible(locator)
            if visible is not None:
                return visible
        return None

    async def _visible_model_button(self, page: Page) -> Locator | None:
        if hasattr(page, "locator"):
            control = page.locator("button[data-input-engine-actionbar-control-key='video-model']")
            visible = await self._first_visible(control)
            if visible is not None:
                return visible
        legacy = await self._first_visible(page.get_by_role("button", name="Seedance 2.0 Fast", exact=True))
        if legacy is not None:
            return legacy
        return await self._first_visible(
            page.get_by_role("button", name=re.compile(r"^(?:Model\s+)?(?:Dreamina Seedance\s+)?(?:Seedance\s+)?(?:2\.0 Fast|2\.5)$", re.I))
        )

    async def _wait_for_model_button(self, page: Page, timeout_seconds: int) -> Locator | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            button = await self._visible_model_button(page)
            if button is not None:
                return button
            await asyncio.sleep(0.25)
        return None

    async def _wait_for_option_menu(self, page: Page, timeout_seconds: int) -> Locator | None:
        return await self._wait_for_first_visible(page.locator("[role='menu'], [role='listbox']"), timeout_seconds)

    async def _click_control(self, locator: Locator) -> None:
        try:
            await locator.click(timeout=10_000, force=True)
        except TypeError:
            # Lightweight test doubles may not accept Playwright keyword args.
            await locator.click()
        except PlaywrightError:
            # Dola controls can briefly be covered while the route hydrates;
            # dispatch a DOM click after the normal click has timed out.
            try:
                await locator.evaluate("element => element.click()")
            except Exception:
                await locator.click(timeout=10_000)

    async def _video_tab_active(self, video_tab: Locator, model: Locator | None, duration_visible: bool) -> bool:
        try:
            aria_selected = await video_tab.get_attribute("aria-selected")
            state = await video_tab.get_attribute("data-state")
            if aria_selected is not None or state is not None:
                return aria_selected == "true" or state == "active"
        except Exception:
            pass
        # Older Dola markup and unit-test doubles do not expose selected state;
        # the video-only model plus duration control is sufficient there.
        return model is not None and duration_visible

    async def _first_visible(self, locator: Locator) -> Locator | None:
        count = await locator.count()
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        return None

    async def _wait_for_first_visible(self, locator: Locator, timeout_seconds: int) -> Locator | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            visible = await self._first_visible(locator)
            if visible is not None:
                return visible
            await asyncio.sleep(0.25)
        return None

    async def _raise_options_error(self, page: Page, network: BrowserNetworkState, message: str) -> None:
        screenshot_path = await self._screenshot(page, "generation-options-mismatch")
        raise DolaBrowserError(message, self._diagnostic(page, network, screenshot_path=screenshot_path), "GENERATION_OPTIONS_MISMATCH")

    def _set_stage(self, network: BrowserNetworkState, stage: str, timeout_seconds: int | None = None) -> None:
        network.stage = stage
        network.stage_started_at = time.monotonic()
        network.timeout_seconds = timeout_seconds

    def _debug_step(
        self,
        network: BrowserNetworkState,
        log_fn: Any | None,
        index: int,
        name: str,
        status: str,
        detail: str = "",
    ) -> None:
        if not network.debug_enabled:
            return
        network.debug_step_index = index
        network.debug_step_name = name
        entry = next((row for row in network.debug_steps if row.get("index") == index), None)
        if entry is None:
            entry = {"index": index, "name": name}
            network.debug_steps.append(entry)
        entry.update(
            {
                "status": status,
                "elapsed_seconds": round(max(0.0, time.monotonic() - network.debug_started_at), 1),
                "detail": sanitize_dola_log_message(detail[:240]) if detail else "",
            }
        )
        if log_fn:
            suffix = f" — {entry['detail']}" if entry["detail"] else ""
            level = "success" if status == "completed" else "error" if status == "failed" else "info"
            log_fn(f"[Browser step {index}/{BROWSER_DEBUG_TOTAL_STEPS}] {name}: {status}{suffix}", level)

    def _fail_current_debug_step(self, network: BrowserNetworkState, log_fn: Any | None, detail: str) -> None:
        if network.debug_step_index and network.debug_step_name:
            self._debug_step(network, log_fn, network.debug_step_index, network.debug_step_name, "failed", detail)

    @staticmethod
    def _browser_auth_cookies(auth_cookies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        filtered = [
            cookie
            for cookie in auth_cookies
            if str(cookie.get("name") or "").strip().lower() not in FRESH_BROWSER_COOKIE_NAMES
        ]
        return filtered, len(auth_cookies) - len(filtered)

    async def _find_video_textbox(self, page: Page) -> Locator | None:
        role_locator = page.get_by_role("textbox")
        count = await role_locator.count()
        for index in range(count):
            candidate = role_locator.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                contenteditable = await candidate.get_attribute("contenteditable")
                class_name = await candidate.get_attribute("class") or ""
                if contenteditable == "true" or "ProseMirror" in class_name:
                    return candidate
        return None

    async def _wait_for_video_textbox(self, page: Page, timeout_seconds: int) -> Locator | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            textbox = await self._find_video_textbox(page)
            if textbox:
                return textbox
            await asyncio.sleep(0.5)
        return None

    async def _duration_control_visible(self, page: Page) -> bool:
        for label in ("5s", "10s", "15s"):
            control = page.get_by_role("button", name=label, exact=True)
            if await self._first_visible(control) is not None:
                return True
        return False

    async def _find_submit_button_candidate(self, page: Page, textbox: Locator) -> Locator | None:
        candidates = (
            page.locator(".send-btn-wrapper > button"),
            page.locator("#flow-end-msg-send"),
            page.locator("button[type='submit']"),
            page.locator("button[aria-label*='send' i]"),
            page.locator("button[aria-label*='submit' i]"),
        )
        for locator in candidates:
            count = await locator.count()
            for index in range(count):
                button = locator.nth(index)
                if await button.is_visible():
                    return button
        return None

    async def _wait_for_submit_button(
        self,
        page: Page,
        textbox: Locator,
        timeout_seconds: int,
        candidate: Locator | None = None,
    ) -> Locator | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if candidate is not None and await candidate.is_visible() and await candidate.is_enabled():
                return candidate
            candidates = [
                page.locator(".send-btn-wrapper > button"),
                page.locator("#flow-end-msg-send"),
                page.locator("button[type='submit']"),
                page.locator("button[aria-label*='send' i]"),
                page.locator("button[aria-label*='submit' i]"),
            ]
            for locator in candidates:
                count = await locator.count()
                for index in range(count):
                    button = locator.nth(index)
                    if await button.is_visible() and await button.is_enabled():
                        return button
            await asyncio.sleep(0.5)
        return None

    async def _visible_dola_elements(self, page: Page) -> list[str]:
        checks = (
            ("video_tab", page.get_by_role("tab", name="Video", exact=True)),
            ("seedance_model", page.locator("button[data-input-engine-actionbar-control-key='video-model']")),
            ("ratio_control", page.get_by_role("button", name="Ratio", exact=True)),
            ("textbox", page.get_by_role("textbox")),
            ("loading_skeleton", page.locator("[class*='skeleton' i]")),
            ("dialog", page.locator("[role='dialog']")),
            ("video", page.locator("video")),
        )
        visible: list[str] = []
        for name, locator in checks:
            count = await locator.count()
            for index in range(min(count, 5)):
                if await locator.nth(index).is_visible():
                    visible.append(name)
                    break
        return visible

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        for selector in ("button:has-text('OK')", "button:has-text('Accept')", "button:has-text('Agree')"):
            button = page.locator(selector).first
            try:
                if await button.count() and await button.is_visible(timeout=1_000):
                    await button.click(timeout=3_000)
                    return
            except PlaywrightError:
                continue

    async def _login_popup_visible(self, page: Page) -> bool:
        heading = page.get_by_text("Log In to Unlock More Features", exact=False)
        count = await heading.count()
        for index in range(count):
            if await heading.nth(index).is_visible():
                return True
        return False

    async def _dismiss_login_popup(
        self,
        page: Page,
        network: BrowserNetworkState,
        log_fn: Any | None = None,
    ) -> bool:
        if not await self._login_popup_visible(page):
            return False
        if log_fn:
            log_fn("Dola login popup detected", "warn")
        close_selectors = (
            "[role='dialog'] .semi-modal-close",
            "[role='dialog'] button[aria-label='Close']",
            ".semi-modal-close",
            "[role='dialog'] [class*='close' i]",
        )
        for _attempt in range(3):
            clicked = False
            for selector in close_selectors:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible():
                            await candidate.click(timeout=3_000)
                            clicked = True
                            break
                    except PlaywrightError:
                        continue
                if clicked:
                    break
            if not clicked:
                try:
                    await page.keyboard.press("Escape")
                except PlaywrightError:
                    pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if not await self._login_popup_visible(page):
                    if log_fn:
                        log_fn("Dola login popup closed", "success")
                        log_fn("Continuing Dola page loading", "info")
                    return True
                await asyncio.sleep(0.2)
        screenshot_path = await self._screenshot(page, "login-required")
        raise DolaBrowserError(
            "Dola login popup could not be closed after three attempts.",
            self._diagnostic(page, network, screenshot_path=screenshot_path),
            "LOGIN_REQUIRED",
        )

    async def _dismiss_modal_overlays(self, page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        close_selectors = (
            ".semi-modal-close",
            ".semi-modal .semi-icon-close",
            ".semi-modal [class*=close]",
            "[role='dialog'] [class*=close]",
            "[class*=modal] [class*=close]",
            "[class*=Modal] [class*=close]",
            ".semi-modal button:has-text('Close')",
            ".semi-modal button:has-text('Cancel')",
            ".semi-modal button:has-text('Not now')",
            "[role='dialog'] button:has-text('Close')",
            "[role='dialog'] button:has-text('Cancel')",
            "[role='dialog'] button:has-text('Not now')",
            "button[aria-label='Close']",
            "[aria-label='Close']",
            "svg[aria-label='Close']",
            "button:has-text('Got it')",
            "button:has-text('Maybe later')",
            "button:has-text('Skip')",
        )
        for selector in close_selectors:
            try:
                locator = page.locator(selector).last
                if await locator.count() and await locator.is_visible(timeout=500):
                    await locator.click(timeout=1_500)
            except Exception:
                continue

    async def _click_ready_video_card(self, page: Page) -> bool:
        ready_text = page.get_by_text("Your video is ready.", exact=False)
        ready_count = await ready_text.count()
        visible_ready: Locator | None = None
        for index in range(ready_count):
            candidate = ready_text.nth(index)
            if await candidate.is_visible():
                visible_ready = candidate
                break
        if visible_ready is None:
            return False
        for locator in (
            visible_ready.locator("xpath=following::*[contains(@class,'video-player-wrapper')][1]"),
            visible_ready.locator("xpath=following::*[contains(@class,'play-icon-wrapper')][1]"),
            visible_ready.locator("xpath=following::video[1]"),
        ):
            try:
                visible = await self._first_visible(locator)
                if visible is not None:
                    await visible.click(timeout=5_000)
                    return True
            except PlaywrightError:
                continue
        return False

    async def _video_src_from_dom(self, page: Page) -> str:
        video = page.locator("video[src], video source[src]")
        try:
            count = await video.count()
            for index in range(count):
                candidate = video.nth(index)
                src = await candidate.get_attribute("src")
                if src:
                    return src
        except Exception:
            return ""
        return ""

    async def _refresh_generation_patch_status(self, page: Page, network: BrowserNetworkState) -> None:
        if not network.duration_patch_expected and not network.model_patch_expected:
            return
        try:
            flags = await page.evaluate(
                """() => ({
                    duration: Boolean(window.__autoDolaDurationPatchApplied),
                    model: Boolean(window.__autoDolaModelPatchApplied),
                })"""
            )
        except Exception:
            # Some accepted requests expose an opaque/streamed body to
            # Playwright. Keep any values already observed from the request.
            return
        if not isinstance(flags, dict):
            return
        network.duration_patch_applied = network.duration_patch_applied or bool(flags.get("duration"))
        network.model_patch_applied = network.model_patch_applied or bool(flags.get("model"))

    async def _wait_for_submit_capture(
        self,
        context: BrowserContext,
        page: Page,
        network: BrowserNetworkState,
        log_fn: Any | None = None,
    ) -> DolaBrowserSubmitResult:
        deadline = time.monotonic() + BROWSER_SUBMIT_CAPTURE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if network.error_code:
                raise DolaBrowserError(self._message_for_error(network), self._diagnostic(page, network))
            conversation_id = network.conversation_id or conversation_id_from_url(page.url)
            request_step = next((row for row in network.debug_steps if row.get("index") == 13), {})
            if network.captured_url and request_step.get("status") != "completed":
                self._debug_step(network, log_fn, 13, "Capture chat/completion request", "completed", endpoint_name(network.captured_url))
            if conversation_id and network.captured_url:
                self._debug_step(network, log_fn, 14, "Verify submitted payload", "started")
                await self._refresh_generation_patch_status(page, network)

                verified_model = network.captured_model or network.selected_model
                model_source = "captured payload" if network.captured_model else "selected UI control"
                verified_ratio = network.captured_ratio or network.selected_ratio
                ratio_source = "captured payload" if network.captured_ratio else "selected UI control"
                if network.captured_duration is not None:
                    verified_duration = network.captured_duration
                    duration_source = "captured payload"
                elif network.duration_patch_expected:
                    verified_duration = network.requested_duration if network.duration_patch_applied else None
                    duration_source = "request patch" if network.duration_patch_applied else "unverified request patch"
                else:
                    verified_duration = network.selected_duration
                    duration_source = "selected UI control"

                duration_mismatch = verified_duration != network.requested_duration
                ratio_mismatch = verified_ratio != network.requested_ratio
                model_mismatch = bool(network.requested_model and verified_model != network.requested_model)
                network.generation_options_verification = (
                    f"model={verified_model or 'unknown'} via {model_source}; "
                    f"duration={verified_duration if verified_duration is not None else 'unknown'}s via {duration_source}; "
                    f"ratio={verified_ratio or 'unknown'} via {ratio_source}"
                )
                if duration_mismatch or ratio_mismatch or model_mismatch:
                    screenshot_path = await self._screenshot(page, "generation-options-mismatch")
                    raise DolaBrowserError(
                        (
                            "Dola submitted different generation options: "
                            f"requested {network.requested_model or 'unknown'} {network.requested_duration}s {network.requested_ratio}, "
                            f"captured {verified_model or 'unknown'} {verified_duration}s {verified_ratio or 'unknown'}. "
                            f"Verification: {network.generation_options_verification}."
                        ),
                        self._diagnostic(page, network, screenshot_path=screenshot_path),
                        "GENERATION_OPTIONS_MISMATCH",
                    )
                self._debug_step(
                    network,
                    log_fn,
                    14,
                    "Verify submitted payload",
                    "completed",
                    network.generation_options_verification,
                )
                network.last_successful_stage = "generation_options_verified"
                if log_fn:
                    log_fn("Generation options verified", "success")
                self._debug_step(network, log_fn, 15, "Capture Dola conversation", "started")
                session = await self._build_dola_session_from_browser(context, network)
                if not network.conversation_id:
                    network.conversation_id = conversation_id
                chat_url = page.url if conversation_id in page.url else f"{DOLA_CHAT_URL}{conversation_id}"
                self._debug_step(network, log_fn, 15, "Capture Dola conversation", "completed", f"conversation *{conversation_id[-8:]}")
                diagnostic = self._diagnostic(page, network)
                diagnostic["chat_url"] = chat_url
                diagnostic["submit_url"] = network.captured_url
                if log_fn:
                    log_fn("Submission captured", "success")
                return DolaBrowserSubmitResult(
                    session=session,
                    conversation_id=conversation_id,
                    conversation_type=network.conversation_type,
                    diagnostic=diagnostic,
                    chat_url=chat_url,
                    submit_url=network.captured_url,
                )
            await asyncio.sleep(0.5)
        screenshot_path = await self._screenshot(page, "submit-not-captured")
        raise DolaBrowserError(
            "Dola received the click, but AUTO-DOLA could not capture the submitted conversation.",
            self._diagnostic(page, network, screenshot_path=screenshot_path),
            "SUBMIT_NOT_CAPTURED",
        )

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
            unwatermarked_url=network.unwatermarked_url or "",
            seen_fallback_apis=set(network.seen_fallback_apis),
        )

    def _capture_request(self, request: Any, network: BrowserNetworkState) -> None:
        if "/chat/completion" not in request.url:
            return
        post_data = request.post_data or ""
        model, duration, ratio = extract_generation_options_from_post_data(post_data)
        network.captured_model = model
        network.captured_duration = duration
        network.captured_ratio = ratio
        network.duration_patch_applied = bool(network.duration_patch_expected and duration == network.requested_duration)
        network.model_patch_applied = bool(network.model_patch_expected and model == network.requested_model)
        network.captured_url = request.url
        network.captured_method = request.method
        network.captured_headers = dict(request.headers)
        network.captured_request = {
            "url": request.url,
            "method": request.method,
            "headers": redact_headers(request.headers),
            "post_data_snippet": sanitize_dola_log_message(post_data[:500]),
            "model": model,
            "duration": duration,
            "ratio": ratio,
            "duration_patch_expected": network.duration_patch_expected,
            "duration_patch_applied": network.duration_patch_applied,
            "model_patch_expected": network.model_patch_expected,
            "model_patch_applied": network.model_patch_applied,
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
        try:
            payload = json_loads_or_empty(text)
        except ValueError:
            payload = {}
        for fallback_api in find_dola_fallback_apis(payload, text):
            if fallback_api in network.seen_fallback_apis:
                continue
            network.seen_fallback_apis.add(fallback_api)
            network.fallback_resolutions_pending += 1
            try:
                raw_url = await self._resolve_browser_unwatermarked_url(response, fallback_api)
                if raw_url:
                    network.unwatermarked_url = raw_url
                    break
            finally:
                network.fallback_resolutions_pending = max(0, network.fallback_resolutions_pending - 1)
        if endpoint == "submit":
            try:
                network.conversation_id, network.conversation_type = parse_conversation_from_stream(text)
            except ValueError:
                pass
        elif endpoint == "play_info":
            download_url = parse_play_info(payload)
            if download_url:
                network.download_url = download_url
                network.vid = vid_from_download_url(download_url)

    async def _resolve_browser_unwatermarked_url(self, response: Any, fallback_api: str) -> str:
        api_url = build_unwatermarked_fallback_url(fallback_api)
        try:
            page = response.request.frame.page
            payload = await page.evaluate(
                """async (url) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 15000);
                    try {
                        const result = await fetch(url, {
                            method: 'GET',
                            credentials: 'omit',
                            headers: {accept: 'application/json,text/plain,*/*'},
                            signal: controller.signal,
                        });
                        if (!result.ok) throw new Error(`HTTP ${result.status}`);
                        return await result.json();
                    } finally {
                        clearTimeout(timer);
                    }
                }""",
                api_url,
            )
            return parse_unwatermarked_video_url(payload) or ""
        except Exception:
            # The page can close while an async response callback is running.
            # Keep a direct, credential-free fallback so the normal HTTP and
            # browser paths resolve the same raw source.
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    result = await client.get(api_url, headers={"accept": "application/json,text/plain,*/*"})
                    result.raise_for_status()
                    return parse_unwatermarked_video_url(result.json()) or ""
            except Exception:
                return ""

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
        except Exception:
            return ""

    def _diagnostic(
        self,
        page: Page,
        network: BrowserNetworkState,
        *,
        screenshot_path: str = "",
        body_snippet: str = "",
    ) -> dict[str, Any]:
        screenshot_filename = Path(screenshot_path).name if screenshot_path else ""
        return {
            "cdp": True,
            "page_url": page.url,
            "manual_url": self.manual_url,
            "stage": network.stage,
            "last_successful_stage": network.last_successful_stage,
            "stage_elapsed_seconds": round(max(0, time.monotonic() - network.stage_started_at), 1),
            "timeout_seconds": network.timeout_seconds,
            "visible_elements": network.visible_elements,
            "endpoint": network.last_endpoint,
            "status_code": network.last_status,
            "error_code": network.error_code,
            "error_msg": network.error_msg,
            "body_snippet": body_snippet or network.last_body_snippet,
            "conversation_id": mask_id(network.conversation_id),
            "vid": mask_id(network.vid),
            "has_download_url": bool(network.download_url),
            "has_unwatermarked_url": bool(network.unwatermarked_url),
            "captured_request": network.captured_request,
            "captured_endpoint": endpoint_name(network.captured_url),
            "requested_duration": network.requested_duration,
            "requested_model": network.requested_model,
            "requested_ratio": network.requested_ratio,
            "visible_duration": network.visible_duration,
            "visible_model": network.visible_model,
            "selected_model": network.selected_model,
            "selected_duration": network.selected_duration,
            "selected_ratio": network.selected_ratio,
            "captured_duration": network.captured_duration,
            "captured_model": network.captured_model,
            "captured_ratio": network.captured_ratio,
            "duration_patch_expected": network.duration_patch_expected,
            "duration_patch_applied": network.duration_patch_applied,
            "model_patch_expected": network.model_patch_expected,
            "model_patch_applied": network.model_patch_applied,
            "generation_options_verification": network.generation_options_verification,
            "images_blocked": network.images_blocked,
            "blocked_image_count": int(network.resource_stats.get("blocked_image_count", 0)),
            "video_tab_visible": network.video_tab_visible,
            "model_visible": network.model_visible,
            "duration_visible": network.duration_visible,
            "ratio_visible": network.ratio_visible,
            "textbox_visible": network.textbox_visible,
            "main_frame_navigation_count": network.main_frame_navigation_count,
            "navigation_request_count": network.navigation_request_count,
            "document_load_count": network.document_load_count,
            "last_navigation_path": network.last_navigation_path,
            "browser_debug_step": network.debug_step_name,
            "browser_debug_step_index": network.debug_step_index,
            "browser_debug_steps": list(network.debug_steps),
            "cookie_names": cookie_names_from_header(network.captured_headers.get("cookie", "")),
            "screenshot_filename": screenshot_filename,
            "screenshot_url": f"/api/video/browser-screenshots/{screenshot_filename}" if screenshot_filename else "",
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


def browser_visible_duration(duration: int) -> int:
    return int(duration) if int(duration) in {5, 10} else 10


def build_browser_video_prompt_text(prompt: str, duration: int, ratio: str) -> str:
    return build_video_prompt_text(prompt, duration, ratio)


def extract_duration_and_ratio_from_post_data(post_data: str) -> tuple[int | None, str]:
    _model, duration, ratio = extract_generation_options_from_post_data(post_data)
    return duration, ratio


def extract_generation_options_from_post_data(post_data: str) -> tuple[str, int | None, str]:
    if not post_data:
        return "", None, ""
    decoded = post_data
    for _ in range(2):
        unquoted = unquote(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    searchable = f"{post_data}\n{decoded}"
    model_match = re.search(r'\\?"(?:model|video_model)\\?"\s*:\s*\\?"([^"\\]+)', searchable)
    duration_match = re.search(
        r'\\?"(?:duration|video_duration|seconds|motion_seconds|video_length)\\?"\s*:\s*(\d+)',
        searchable,
    )
    ratio_match = re.search(r'\\?"(?:ratio|aspect_ratio)\\?"\s*:\s*\\?"([^"\\]+)', searchable)
    model = model_match.group(1) if model_match else ""
    try:
        model = normalize_video_model(model)
    except ValueError:
        model = model.strip().lower()
    duration = int(duration_match.group(1)) if duration_match else None
    ratio = ratio_match.group(1) if ratio_match else ""
    return model, duration, ratio


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
    labels = (
        ("error_type", "Error type"),
        ("user_message", "Message"),
        ("stage", "Failed stage"),
        ("last_successful_stage", "Last successful stage"),
        ("browser_debug_step_index", "Browser debug step"),
        ("browser_debug_step", "Browser debug action"),
        ("stage_elapsed_seconds", "Stage elapsed"),
        ("timeout_seconds", "Timeout"),
        ("page_url", "Page"),
        ("visible_elements", "Visible elements"),
        ("captured_endpoint", "Captured endpoint"),
        ("status_code", "HTTP status"),
        ("error_code", "Dola error code"),
        ("error_msg", "Dola error"),
        ("conversation_id", "Conversation"),
        ("vid", "Video id"),
        ("requested_model", "Requested model"),
        ("requested_duration", "Requested duration"),
        ("requested_ratio", "Requested ratio"),
        ("visible_model", "Visible model"),
        ("selected_model", "Selected model"),
        ("selected_duration", "Selected duration"),
        ("selected_ratio", "Selected ratio"),
        ("captured_model", "Captured model"),
        ("captured_duration", "Captured duration"),
        ("captured_ratio", "Captured ratio"),
        ("duration_patch_expected", "Duration patch expected"),
        ("duration_patch_applied", "Duration patch applied"),
        ("model_patch_expected", "Model patch expected"),
        ("model_patch_applied", "Model patch applied"),
        ("generation_options_verification", "Generation options verification"),
        ("images_blocked", "Images blocked"),
        ("blocked_image_count", "Blocked image count"),
        ("video_tab_visible", "Video tab visible"),
        ("model_visible", "Model visible"),
        ("duration_visible", "Duration visible"),
        ("ratio_visible", "Ratio visible"),
        ("textbox_visible", "Textbox visible"),
        ("main_frame_navigation_count", "Main-frame navigation events"),
        ("navigation_request_count", "Document navigation requests"),
        ("document_load_count", "DOMContentLoaded count"),
        ("last_navigation_path", "Last navigation path"),
        ("browser_proxy_host", "Proxy"),
        ("screenshot_url", "Screenshot"),
        ("body_snippet", "Response"),
    )
    lines = ["Dola browser diagnostic:"]
    for key, label in labels:
        value = diagnostic.get(key)
        if value in (None, "", [], {}):
            continue
        suffix = "s" if key in {"stage_elapsed_seconds", "timeout_seconds"} else ""
        lines.append(f"  {label}: {value}{suffix}")
    steps = diagnostic.get("browser_debug_steps") or []
    if steps:
        lines.append("  Browser step history:")
        for step in steps:
            detail = f" — {step.get('detail')}" if step.get("detail") else ""
            lines.append(
                f"    {step.get('index')}/{BROWSER_DEBUG_TOTAL_STEPS} "
                f"{step.get('name')}: {step.get('status')} at {step.get('elapsed_seconds')}s{detail}"
            )
    return "\n".join(lines)

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from uuid import UUID

import httpx
from redis import Redis
from rq import Worker
from sqlmodel import Session, select

from app.config import settings
from app.database import engine, init_db
from app.models import Artifact, ItemStatus, Job, JobItem, JobKind, JobStatus, utcnow
from app.services.cookie_snapshots import (
    append_raw_response_event,
    create_cookie_snapshot,
    dola_session_from_cookie_snapshot,
    latest_browser_snapshot_for_item,
    mark_cookie_snapshot_conversation,
    update_cookie_snapshot,
)
from app.services.dola import DolaClient, DolaSubmissionError, DolaTerminalGenerationError, VIDEO_POLL_ATTEMPTS, build_dola_payload, format_diagnostic, is_terminal_video_failure
from app.services.dola_browser import DolaBrowserClient, DolaBrowserError, format_browser_diagnostic
from app.services.images import generate_image
from app.services.jobs import add_artifact, log, mark_item, recompute_job
from app.services.media import clean_video, safe_filename
from app.services.settings import load_app_settings
from app.services.tts import synthesize
from app.services.vpn import choose_vpn_config, choose_vpn_username, vpn_config_path

logger = logging.getLogger(__name__)
MAX_DOLA_PARALLEL = 50
BROWSER_SUBMIT_PARALLEL = 5
HIGH_DEMAND_MIN_RETRIES = 8
HIGH_DEMAND_BACKOFF_SECONDS = (30, 45, 60, 90, 120, 150, 180)
BROWSER_FALLBACK_ERROR_CODES = {710022002, 710022017}


class JobCancelled(RuntimeError):
    """Raised inside a worker item when the user force-stops a job."""


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str, diagnostic: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic or {}


def should_fallback_to_browser(exc: DolaSubmissionError) -> bool:
    if exc.diagnostic.get("error_code") in BROWSER_FALLBACK_ERROR_CODES:
        return True
    message = str(exc).lower()
    return any(fragment in message for fragment in ("common invalid param", "conversation_id", "anonymous session"))


def effective_video_parallel(value: object, max_parallel: int = MAX_DOLA_PARALLEL) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = 1
    return max(1, min(requested, max_parallel))


def process_job(job_id: str) -> None:
    asyncio.run(_process_job(UUID(job_id)))


async def _process_job(job_id: UUID) -> None:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return
        if job.status == JobStatus.cancelled:
            log(session, "Worker skipped cancelled job before start.", "warn", job.id)
            return
        job.status = JobStatus.running
        job.updated_at = utcnow()
        session.add(job)
        session.commit()
        try:
            if job.kind == JobKind.video:
                await process_video(session, job)
            elif job.kind == JobKind.image:
                await process_images(session, job)
            elif job.kind == JobKind.tts:
                await process_tts(session, job)
            recompute_job(session, job.id)
        except Exception as exc:
            job.status = JobStatus.failed
            job.error = str(exc)
            job.updated_at = utcnow()
            session.add(job)
            session.commit()
            log(session, f"Job failed: {exc}", "error", job.id)


async def process_video(session: Session, job: Job) -> None:
    app_settings = load_app_settings(session, include_secrets=True)
    config = job.config_json
    output_dir = Path(config.get("save_folder") or app_settings.get("output_dir") or settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    vpn_enabled = bool(app_settings.get("vpn_enabled"))
    proxy_url = app_settings.get("proxy_url", "") if app_settings.get("proxy_enabled") and not vpn_enabled else ""
    dola_mode = str(app_settings.get("dola_mode") or settings.dola_mode or "hybrid").lower()
    if dola_mode not in {"direct", "browser", "hybrid"}:
        dola_mode = "hybrid"
    requested_duration = int(config.get("duration", 10))
    effective_dola_mode = "browser" if dola_mode == "hybrid" and requested_duration == 15 else dola_mode
    submit_client = DolaClient(app_settings.get("dola_auth_cookies", settings.dola_auth_cookies), settings.dola_default_region, proxy=proxy_url)
    poll_client = DolaClient(app_settings.get("dola_auth_cookies", settings.dola_auth_cookies), settings.dola_default_region)
    browser_client = DolaBrowserClient(proxy_url=proxy_url)
    items = session.exec(select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.created_at.asc(), JobItem.id.asc())).all()
    item_numbers = {item.id: index + 1 for index, item in enumerate(items)}
    requested_parallel = config.get("parallel", 1)
    parallel = effective_video_parallel(requested_parallel)
    log(session, f"Video concurrency requested: {requested_parallel}", "info", job.id)
    log(session, f"Video concurrency effective: {parallel}", "info", job.id)
    log(session, f"Video settings: duration={requested_duration}, ratio={config.get('ratio', '9:16')}, mode={effective_dola_mode}, submit_proxy_enabled={bool(proxy_url)}, vpn_enabled={vpn_enabled}, polling_proxy_enabled=False", "info", job.id)
    max_retries = max(int(config.get("max_retries", 3)), HIGH_DEMAND_MIN_RETRIES)
    semaphore = asyncio.Semaphore(parallel)
    browser_submit_semaphore = asyncio.Semaphore(1 if vpn_enabled else min(BROWSER_SUBMIT_PARALLEL, parallel))

    def ensure_not_cancelled(session_local: Session, item_id: UUID | None = None) -> JobItem | None:
        current_job = session_local.get(Job, job.id)
        if current_job is None or current_job.status == JobStatus.cancelled:
            current_item = session_local.get(JobItem, item_id) if item_id else None
            if current_item and current_item.status != ItemStatus.cancelled:
                mark_item(session_local, current_item, ItemStatus.cancelled, "Force stopped")
            raise JobCancelled("Force stopped")
        return session_local.get(JobItem, item_id) if item_id else None

    async def _run_item(item: JobItem) -> None:
        async with semaphore:
            session_local = Session(engine)
            try:
                db_item = session_local.get(JobItem, item.id)
                db_job = session_local.get(Job, job.id)
                if not db_item or (db_job and db_job.status == JobStatus.cancelled):
                    if db_item:
                        mark_item(session_local, db_item, ItemStatus.cancelled, "Force stopped")
                    return
                last_error = ""
                last_diagnostic: dict = {}
                video_label = f"Video {item_numbers.get(db_item.id, 0)} | {db_item.id.hex[:8]}"

                def item_log(message: str, level: str = "info") -> None:
                    log(session_local, f"[{video_label}] {message}", level, job.id)

                def progress_log() -> Callable[[str, str], None]:
                    def _log(message: str, level: str = "info") -> None:
                        if message.startswith(("Polling video id ", "Polling play_info ")):
                            mark_item(session_local, db_item, ItemStatus.running, message)
                        item_log(message, level)

                    return _log

                def job_cancelled_or_missing() -> bool:
                    current_job = session_local.get(Job, job.id)
                    return current_job is None or current_job.status == JobStatus.cancelled

                async def wait_before_retry(delay_seconds: float) -> bool:
                    remaining = delay_seconds
                    while remaining > 0:
                        if job_cancelled_or_missing():
                            mark_item(session_local, db_item, ItemStatus.cancelled, "Force stopped")
                            item_log("Force stopped. Worker stopped processing this item.", "warn")
                            return False
                        await asyncio.sleep(min(1.0, remaining))
                        remaining -= 1.0
                    return True

                async def save_video_artifact(download_url: str, vid: str, action_prefix: str) -> None:
                    filename = unique_video_filename(output_dir, vid, db_item.title)
                    raw_path = output_dir / filename.replace(".mp4", "_raw.mp4")
                    final_path = output_dir / filename
                    mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Downloading MP4")
                    download_info = await download_file(download_url, raw_path, cancel_fn=job_cancelled_or_missing)
                    item_log(f"Downloaded MP4 bytes={download_info['bytes_written']} path={raw_path}", "success")
                    ensure_not_cancelled(session_local, db_item.id)
                    artifact_path = save_downloaded_video(
                        raw_path,
                        final_path,
                        str(config.get("save_mode") or "final").lower(),
                        bool(config.get("clean_watermark", True)),
                        lambda message, level="info": item_log(message, level),
                        lambda action: mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}{action}"),
                    )
                    ensure_not_cancelled(session_local, db_item.id)
                    artifact = Artifact(job_id=job.id, item_id=db_item.id, kind="video", path=str(artifact_path), filename=artifact_path.name, mime_type="video/mp4", size_bytes=artifact_path.stat().st_size)
                    add_artifact(session_local, artifact, db_item)
                    mark_item(session_local, db_item, ItemStatus.completed, artifact_path.name)
                    item_log(f"Completed video: {artifact_path.name}", "success")

                async def run_browser_generation(action_prefix: str, attempt: int) -> None:
                    ensure_not_cancelled(session_local, db_item.id)
                    mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Submitting through Dola browser")
                    item_log("Connected to Dola browser fallback.", "info")
                    browser_slot_id = ""
                    browser_profile_dir = ""
                    browser_profile_retained = False
                    browser_completed = False
                    vpn_connected = False

                    async def cleanup_browser_slot(reason: str, *, delete_profile: bool = True) -> None:
                        nonlocal browser_slot_id, browser_profile_retained
                        if not browser_slot_id:
                            return
                        item_log(f"Closing browser slot after {reason}: {browser_slot_id}", "info")
                        item_log("Deleting browser profile" if delete_profile else "Keeping browser profile for fallback", "info")
                        if await browser_client.close_slot(browser_slot_id, delete_profile=delete_profile):
                            item_log("Browser profile deleted" if delete_profile else "Browser submitted and closed", "success")
                            if not delete_profile:
                                browser_profile_retained = True
                            browser_slot_id = ""
                        else:
                            item_log(f"Browser cleanup failed: {browser_slot_id}", "error")

                    async def cleanup_retained_profile() -> None:
                        nonlocal browser_profile_retained
                        if not browser_profile_retained or not browser_profile_dir:
                            return
                        if await browser_client.delete_profile(browser_profile_dir):
                            item_log("Browser profile deleted", "success")
                        else:
                            item_log(f"Browser profile cleanup failed: {browser_profile_dir}", "warn")
                        browser_profile_retained = False

                    def browser_submit_log(message: str, level: str = "info") -> None:
                        progress_prefixes = (
                            "Waiting for Dola page ",
                            "Video tab clicked",
                            "Video controls ready",
                            "Selecting ratio ",
                            "Ratio selected: ",
                            "Selecting duration ",
                            "Duration selected: ",
                            "Generation options verified",
                            "Entering prompt",
                            "Prompt verified",
                            "Waiting for submit button",
                            "Submitting prompt",
                            "Submission captured",
                        )
                        if message.startswith(progress_prefixes):
                            mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}{message}")
                        item_log(f"Dola browser: {message}", level)

                    async with browser_submit_semaphore:
                        if vpn_enabled:
                            if not app_settings.get("vpn_password"):
                                raise DolaBrowserError("OpenVPN password is missing.", {"error_type": "VPN_AUTH_FAILED"}, "VPN_AUTH_FAILED")
                            try:
                                vpn_config = choose_vpn_config()
                                vpn_username = choose_vpn_username(str(app_settings.get("vpn_usernames") or ""))
                            except ValueError as exc:
                                raise DolaBrowserError(str(exc), {"error_type": str(exc)}, str(exc)) from exc
                            item_log(f"Connecting OpenVPN: config={vpn_config['name']}", "info")
                            vpn_status = await browser_client.vpn_connect(
                                config_path=str(vpn_config_path(vpn_config["name"])),
                                config_name=str(vpn_config["name"]),
                                username=vpn_username,
                                password=str(app_settings.get("vpn_password") or ""),
                            )
                            vpn_connected = True
                            item_log(f"VPN connected: config={vpn_status.get('config_name')}, user={vpn_status.get('username_masked')}, ip={vpn_status.get('ip')}", "success")
                        try:
                            browser_result = await browser_client.submit_and_capture_session(
                                db_item.prompt,
                                int(config.get("duration", 10)),
                                str(config.get("ratio", "9:16")),
                                log_fn=browser_submit_log,
                            )
                        except DolaBrowserError as exc:
                            if vpn_enabled:
                                exc.error_type = "DOLA_SUBMIT_FAILED_AFTER_VPN"
                                exc.diagnostic["error_type"] = "DOLA_SUBMIT_FAILED_AFTER_VPN"
                                if vpn_connected:
                                    await browser_client.vpn_disconnect()
                                    vpn_connected = False
                                    item_log("OpenVPN disconnected", "success")
                            raise
                        except Exception:
                            if vpn_enabled and vpn_connected:
                                await browser_client.vpn_disconnect()
                                vpn_connected = False
                                item_log("OpenVPN disconnected", "success")
                            raise
                    browser_slot_id = browser_result.slot_id
                    browser_profile_dir = str(browser_result.diagnostic.get("profile_dir") or "")
                    conversation_hint = browser_result.conversation_id[-8:] if len(browser_result.conversation_id) > 8 else browser_result.conversation_id
                    try:
                        item_log(f"Browser submitted Dola prompt: conversation_id=*{conversation_hint}, conversation_type={browser_result.conversation_type}.", "success")
                        browser_snapshot = create_cookie_snapshot(
                            session_local,
                            job.id,
                            db_item.id,
                            attempt,
                            browser_result.session,
                            source="browser",
                            chat_url=browser_result.chat_url,
                            extra_metadata={
                                "submit_url": browser_result.submit_url,
                                "slot_id": browser_slot_id,
                                "profile_dir": browser_profile_dir,
                                "cdp_port": browser_result.diagnostic.get("cdp_port"),
                                "profile_retained": not vpn_enabled,
                                "vpn_enabled": vpn_enabled,
                                "requested_duration": browser_result.diagnostic.get("requested_duration"),
                                "visible_duration": browser_result.diagnostic.get("visible_duration"),
                                "captured_duration": browser_result.diagnostic.get("captured_duration"),
                                "captured_ratio": browser_result.diagnostic.get("captured_ratio"),
                                "duration_patch_expected": browser_result.diagnostic.get("duration_patch_expected"),
                                "duration_patch_applied": browser_result.diagnostic.get("duration_patch_applied"),
                                "captured_endpoint": browser_result.diagnostic.get("captured_endpoint"),
                            },
                            extra_payload={
                                "submit_url": browser_result.submit_url,
                                "chat_url": browser_result.chat_url,
                                "captured_request": browser_result.diagnostic.get("captured_request") or {},
                            },
                        )
                        mark_cookie_snapshot_conversation(session_local, job.id, browser_snapshot["snapshot_id"], browser_result.conversation_id, browser_result.conversation_type)
                        item_log(f"Browser session saved: chat_url={browser_result.chat_url}, cookie_snapshot_id={browser_snapshot['snapshot_id']}", "success")
                        item_log("Captured browser session. Closing browser and switching to direct HTTP poll/download.", "info")
                        item_log(format_browser_diagnostic(browser_result.diagnostic), "info")
                        await cleanup_browser_slot("submit", delete_profile=vpn_enabled)
                        if vpn_enabled:
                            if await browser_client.vpn_disconnect():
                                item_log("OpenVPN disconnected", "success")
                            vpn_connected = False
                        else:
                            item_log("Profile retained for fallback", "info")
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Polling video id 1/{VIDEO_POLL_ATTEMPTS}")
                        item_log("Polling direct HTTP 1/250", "info")
                        item_log("Polling video id with browser session over direct HTTP (proxy disabled).", "info")

                        async def run_browser_download_fallback(reason: str) -> bool:
                            nonlocal browser_completed, browser_profile_retained
                            if vpn_enabled or not browser_profile_dir:
                                return False
                            item_log(f"Direct polling failed, reopening saved browser profile: {reason}", "warn")
                            mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Reopening saved browser profile")

                            def browser_fallback_log(message: str, level: str = "info") -> None:
                                if message.startswith(("Browser says video ready", "Opened ready", "Capturing play_info")):
                                    mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}{message}")
                                item_log(f"Dola browser fallback: {message}", level)

                            browser_download = await browser_client.reopen_profile_and_wait_for_ready_download(
                                profile_dir=browser_profile_dir,
                                chat_url=browser_result.chat_url,
                                conversation_id=browser_result.conversation_id,
                                log_fn=browser_fallback_log,
                                timeout_seconds=240,
                            )
                            browser_profile_retained = False
                            update_cookie_snapshot(
                                session_local,
                                job.id,
                                browser_snapshot["snapshot_id"],
                                metadata_updates={
                                    "browser_ready_detected": True,
                                    "download_captured_from": "browser_ready_card",
                                    "vid": browser_download.vid,
                                    "has_download_url": bool(browser_download.download_url),
                                },
                                payload_updates={
                                    "browser_ready_detected": True,
                                    "download_captured_from": "browser_ready_card",
                                    "vid": browser_download.vid,
                                    "download_url": browser_download.download_url,
                                },
                            )
                            item_log(format_browser_diagnostic(browser_download.diagnostic), "info")
                            item_log("Browser fallback captured download", "success")
                            await save_video_artifact(browser_download.download_url, browser_download.vid, action_prefix)
                            browser_completed = True
                            return True

                        vid = await poll_client.poll_video_id(
                            browser_result.session,
                            browser_result.conversation_id,
                            browser_result.conversation_type,
                            max_attempts=VIDEO_POLL_ATTEMPTS,
                            log_fn=lambda message, level: progress_log()(f"Dola browser session: {message}" if not message.startswith("Polling ") else message, level),
                            cancel_fn=job_cancelled_or_missing,
                        )
                        if not vid:
                            if await run_browser_download_fallback("video id not returned"):
                                return
                            raise RuntimeError("Dola video ready/download URL not captured.")
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Polling play_info 1/200")
                        item_log("Polling play_info with browser session over direct HTTP (proxy disabled).", "info")
                        download_url = await poll_client.poll_download_url(
                            browser_result.session,
                            vid,
                            cancel_fn=job_cancelled_or_missing,
                            log_fn=lambda message, level: progress_log()(f"Dola browser session: {message}" if not message.startswith("Polling ") else message, level),
                        )
                        if not download_url:
                            if await run_browser_download_fallback("play_info URL not returned"):
                                return
                            raise RuntimeError("Dola browser session did not return play_info URL.")
                        await save_video_artifact(download_url, vid, action_prefix)
                        browser_completed = True
                    finally:
                        await cleanup_browser_slot("success" if browser_completed else "rejection", delete_profile=True)
                        await cleanup_retained_profile()
                        if vpn_connected:
                            if await browser_client.vpn_disconnect():
                                item_log("OpenVPN disconnected", "success")

                for attempt in range(1, max_retries + 1):
                    last_diagnostic = {}
                    try:
                        ensure_not_cancelled(session_local, db_item.id)
                        action_prefix = f"[{attempt}/{max_retries}] " if max_retries > 1 else ""
                        if effective_dola_mode == "browser":
                            await run_browser_generation(action_prefix, attempt)
                            return
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Building Dola session")
                        dola_session = await submit_client.build_session()
                        ensure_not_cancelled(session_local, db_item.id)
                        cookie_snapshot = create_cookie_snapshot(session_local, job.id, db_item.id, attempt, dola_session)

                        def record_raw_response(response_type: str, response_attempt: int, status_code: int, body: str) -> None:
                            try:
                                append_raw_response_event(
                                    session_local,
                                    job.id,
                                    cookie_snapshot["snapshot_id"],
                                    response_type,
                                    response_attempt,
                                    status_code,
                                    body,
                                )
                            except Exception as exc:
                                logger.warning("Could not persist Dola raw %s response metadata for job %s: %s", response_type, job.id, exc)

                        if not dola_session.has_auth_cookies:
                            item_log("Using anonymous Dola session with fresh public cookies.", "info")
                        payload = build_dola_payload(dola_session.payload_template, db_item.prompt, config.get("duration", 10), config.get("ratio", "9:16"))
                        ensure_not_cancelled(session_local, db_item.id)
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Submitting Seedance request")
                        submit_result = await submit_client.submit(dola_session, payload, raw_response_fn=record_raw_response, attempt=attempt)
                        ensure_not_cancelled(session_local, db_item.id)
                        conversation_id = submit_result.conversation_id
                        conversation_type = submit_result.conversation_type
                        conversation_hint = conversation_id[-8:] if len(conversation_id) > 8 else conversation_id
                        mark_cookie_snapshot_conversation(session_local, job.id, cookie_snapshot["snapshot_id"], conversation_id, conversation_type)
                        item_log(f"Dola accepted Seedance request: conversation_id=*{conversation_hint}, conversation_type={conversation_type}.", "success")
                        for assistant_message in submit_result.assistant_messages:
                            if is_terminal_video_failure(assistant_message):
                                item_log(f"Dola: {assistant_message}", "warn")
                                raise DolaTerminalGenerationError(f"Dola rejected this prompt: {assistant_message[:500]}")
                            item_log(f"Dola: {assistant_message}", "info")
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Processing video request")
                        vid = await poll_client.poll_video_id(
                            dola_session,
                            conversation_id,
                            conversation_type,
                            log_fn=lambda message, level: progress_log()(f"Dola: {message}" if not message.startswith("Polling ") else message, level),
                            raw_response_fn=record_raw_response,
                            cancel_fn=job_cancelled_or_missing,
                        )
                        if job_cancelled_or_missing():
                            current_item = session_local.get(JobItem, db_item.id)
                            if current_item:
                                mark_item(session_local, current_item, ItemStatus.cancelled, "Force stopped")
                            return
                        if not vid:
                            if effective_dola_mode == "hybrid":
                                item_log("Direct Dola did not return video id. Falling back to browser.", "warn")
                                await run_browser_generation(action_prefix, attempt)
                                return
                            raise RuntimeError("Dola did not return video id.")
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Polling download URL")
                        download_url = await poll_client.poll_download_url(
                            dola_session,
                            vid,
                            raw_response_fn=record_raw_response,
                            cancel_fn=job_cancelled_or_missing,
                            log_fn=lambda message, level: progress_log()(f"Dola: {message}" if not message.startswith("Polling ") else message, level),
                        )
                        ensure_not_cancelled(session_local, db_item.id)
                        if not download_url:
                            if effective_dola_mode == "hybrid":
                                item_log("Direct Dola did not return video download URL. Falling back to browser.", "warn")
                                await run_browser_generation(action_prefix, attempt)
                                return
                            raise RuntimeError("Dola did not return play_info URL.")
                        await save_video_artifact(download_url, vid, action_prefix)
                        return
                    except JobCancelled:
                        current_item = session_local.get(JobItem, db_item.id)
                        if current_item and current_item.status != ItemStatus.cancelled:
                            mark_item(session_local, current_item, ItemStatus.cancelled, "Force stopped")
                        item_log("Force stopped. Worker stopped processing this item.", "warn")
                        return
                    except DolaSubmissionError as exc:
                        last_error = str(exc)
                        item_log(last_error, "error")
                        item_log(format_diagnostic(exc.diagnostic), "debug")
                        if effective_dola_mode == "hybrid" and should_fallback_to_browser(exc):
                            try:
                                item_log(f"Direct Dola failed with {last_error}. Falling back to browser.", "warn")
                                await run_browser_generation(action_prefix, attempt)
                                return
                            except DolaBrowserError as browser_exc:
                                last_error = str(browser_exc)
                                last_diagnostic = browser_exc.diagnostic
                                item_log(format_browser_diagnostic(browser_exc.diagnostic), "error")
                                if attempt >= max_retries:
                                    break
                        if attempt < max_retries:
                            if exc.diagnostic.get("error_code") == 710022002:
                                delay_seconds = HIGH_DEMAND_BACKOFF_SECONDS[min(attempt - 1, len(HIGH_DEMAND_BACKOFF_SECONDS) - 1)]
                                item_log(f"Attempt {attempt} hit Dola high demand. Waiting {delay_seconds}s before retry...", "warn")
                            else:
                                delay_seconds = 3
                                item_log(f"Attempt {attempt} failed for '{db_item.prompt[:40]}': {exc}. Retrying...", "warn")
                            if not await wait_before_retry(delay_seconds):
                                return
                    except DolaTerminalGenerationError as exc:
                        last_error = str(exc)
                        mark_item(session_local, db_item, ItemStatus.failed, "Prompt flagged", last_error)
                        item_log(last_error, "error")
                        return
                    except DolaBrowserError as exc:
                        last_error = str(exc)
                        last_diagnostic = exc.diagnostic
                        item_log(last_error, "error")
                        item_log(format_browser_diagnostic(exc.diagnostic), "error")
                        if attempt < max_retries:
                            item_log(f"Attempt {attempt} failed in Dola browser: {exc}. Retrying...", "warn")
                            if not await wait_before_retry(10):
                                return
                    except DownloadError as exc:
                        last_error = str(exc)
                        last_diagnostic = {"error_type": exc.code, **exc.diagnostic}
                        mark_item(session_local, db_item, ItemStatus.failed, exc.code, last_error, diagnostic=last_diagnostic)
                        item_log(last_error, "error")
                        item_log(f"Download diagnostic: {last_diagnostic}", "error")
                        return
                    except Exception as exc:
                        last_error = str(exc)
                        if attempt < max_retries:
                            item_log(f"Attempt {attempt} failed for '{db_item.prompt[:40]}': {exc}. Retrying...", "warn")
                            if not await wait_before_retry(3):
                                return
                mark_item(session_local, db_item, ItemStatus.failed, "Failed", last_error, diagnostic=last_diagnostic)
                item_log(f"Item failed after {max_retries} attempts: {last_error}", "error")
            finally:
                session_local.close()

    try:
        await asyncio.gather(*[_run_item(item) for item in items])
    finally:
        await submit_client.aclose()
        await poll_client.aclose()
        await browser_client.close()


def resume_video_item_poll(job_id: str, item_id: str) -> None:
    asyncio.run(_resume_video_item_poll(UUID(job_id), UUID(item_id)))


async def _resume_video_item_poll(job_id: UUID, item_id: UUID) -> None:
    session = Session(engine)
    app_settings = load_public_settings(session)
    client: DolaClient | None = None
    try:
        job = session.get(Job, job_id)
        item = session.get(JobItem, item_id)
        if not job or job.kind != JobKind.video:
            raise RuntimeError("Video job not found.")
        if not item or item.job_id != job.id:
            raise RuntimeError("Video item not found.")
        if item.artifact_id:
            log(session, f"[Resume poll | {item.id.hex[:8]}] Item already has an artifact. Skipping resume.", "info", job.id)
            return

        snapshot = latest_browser_snapshot_for_item(session, job.id, item.id)
        if not snapshot:
            raise RuntimeError("No saved browser session exists for this video item.")

        dola_session, payload = dola_session_from_cookie_snapshot(snapshot)
        conversation_id = str(payload.get("conversation_id") or "")
        conversation_type = int(payload.get("conversation_type") or snapshot.get("conversation_type") or 3)
        if not conversation_id:
            raise RuntimeError("Saved browser session is missing conversation_id.")

        job.status = JobStatus.running
        job.updated_at = utcnow()
        session.add(job)
        session.commit()
        mark_item(session, item, ItemStatus.running, "Resume polling from saved browser session")
        log(session, f"[Resume poll | {item.id.hex[:8]}] Resume polling from saved browser session: chat_url={payload.get('chat_url') or snapshot.get('chat_url')}", "info", job.id)
        log(session, f"[Resume poll | {item.id.hex[:8]}] Polling over direct HTTP (proxy disabled).", "info", job.id)

        client = DolaClient(app_settings.get("dola_auth_cookies", settings.dola_auth_cookies), settings.dola_default_region)

        def record_raw_response(response_type: str, response_attempt: int, status_code: int, body: str) -> None:
            try:
                append_raw_response_event(session, job.id, str(snapshot["snapshot_id"]), response_type, response_attempt, status_code, body)
            except Exception as exc:
                logger.warning("Could not persist resumed Dola %s response metadata for job %s: %s", response_type, job.id, exc)

        def progress_log(message: str, level: str = "info") -> None:
            if message.startswith(("Polling video id ", "Polling play_info ")):
                mark_item(session, item, ItemStatus.running, message)
            log(session, f"[Resume poll | {item.id.hex[:8]}] {message}", level, job.id)

        vid = await client.poll_video_id(
            dola_session,
            conversation_id,
            conversation_type,
            max_attempts=VIDEO_POLL_ATTEMPTS,
            log_fn=progress_log,
            raw_response_fn=record_raw_response,
        )
        if not vid:
            raise RuntimeError("Saved browser session did not return video id.")
        download_url = await client.poll_download_url(
            dola_session,
            vid,
            log_fn=progress_log,
            raw_response_fn=record_raw_response,
        )
        if not download_url:
            raise RuntimeError("Saved browser session did not return play_info URL.")

        config = job.config_json
        output_dir = Path(config.get("save_folder") or app_settings.get("output_dir") or settings.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = unique_video_filename(output_dir, vid, item.title)
        raw_path = output_dir / filename.replace(".mp4", "_raw.mp4")
        final_path = output_dir / filename
        save_mode = str(config.get("save_mode") or "final").lower()
        mark_item(session, item, ItemStatus.running, "Downloading MP4")
        download_info = await download_file(download_url, raw_path)
        log(session, f"[Resume poll | {item.id.hex[:8]}] Downloaded MP4 bytes={download_info['bytes_written']} path={raw_path}", "success", job.id)
        artifact_path = save_downloaded_video(
            raw_path,
            final_path,
            save_mode,
            bool(config.get("clean_watermark", True)),
            lambda message, level="info": log(session, f"[Resume poll | {item.id.hex[:8]}] {message}", level, job.id),
            lambda action: mark_item(session, item, ItemStatus.running, action),
        )

        artifact = Artifact(job_id=job.id, item_id=item.id, kind="video", path=str(artifact_path), filename=artifact_path.name, mime_type="video/mp4", size_bytes=artifact_path.stat().st_size)
        add_artifact(session, artifact, item)
        mark_item(session, item, ItemStatus.completed, artifact_path.name)
        log(session, f"[Resume poll | {item.id.hex[:8]}] Completed video: {artifact_path.name}", "success", job.id)
    except Exception as exc:
        job = session.get(Job, job_id)
        item = session.get(JobItem, item_id)
        if job and item:
            mark_item(session, item, ItemStatus.failed, "Resume poll failed", str(exc))
            log(session, f"[Resume poll | {item.id.hex[:8]}] Resume poll failed: {exc}", "error", job.id)
        else:
            logger.error("Resume poll failed for job=%s item=%s: %s", job_id, item_id, exc)
    finally:
        if client:
            await client.aclose()
        recompute_job(session, job_id)
        session.close()


async def process_images(session: Session, job: Job) -> None:
    app_settings = load_public_settings(session)
    config = job.config_json
    output_dir = Path(config.get("output_folder") or app_settings.get("output_dir") or settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in session.exec(select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.created_at.asc(), JobItem.id.asc())).all():
        try:
            mark_item(session, item, ItemStatus.running, "Generating image")
            path = output_dir / safe_filename(f"image-{item.id.hex[:8]}", ".png")
            await generate_image(item.prompt, config.get("aspect_ratio", "1:1"), app_settings.get("yousmind_api_key", ""), path)
            artifact = Artifact(job_id=job.id, item_id=item.id, kind="image", path=str(path), filename=path.name, mime_type="image/png", size_bytes=path.stat().st_size)
            add_artifact(session, artifact, item)
            mark_item(session, item, ItemStatus.completed, path.name)
        except Exception as exc:
            mark_item(session, item, ItemStatus.failed, "Failed", str(exc))


async def process_tts(session: Session, job: Job) -> None:
    config = job.config_json
    output_dir = Path(config.get("output_folder") or settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in session.exec(select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.created_at.asc(), JobItem.id.asc())).all():
        try:
            mark_item(session, item, ItemStatus.running, "Synthesizing speech")
            path = output_dir / safe_filename(f"tts-{item.id.hex[:8]}", ".mp3")
            await synthesize(item.prompt, config.get("voice", settings.tts_default_voice), path)
            artifact = Artifact(job_id=job.id, item_id=item.id, kind="audio", path=str(path), filename=path.name, mime_type="audio/mpeg", size_bytes=path.stat().st_size)
            add_artifact(session, artifact, item)
            mark_item(session, item, ItemStatus.completed, path.name)
        except Exception as exc:
            mark_item(session, item, ItemStatus.failed, "Failed", str(exc))


async def download_file(url: str, path: Path, cancel_fn: Callable[[], bool] | None = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    part_path = path.with_name(f"{path.name}.part")
    part_path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    bytes_written = 0
    status_code = 0
    content_type = ""
    content_length = ""
    try:
        async with httpx.AsyncClient(timeout=60, verify=False, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                status_code = response.status_code
                content_type = response.headers.get("content-type", "")
                content_length = response.headers.get("content-length", "")
                response.raise_for_status()
                with part_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if cancel_fn and cancel_fn():
                            raise JobCancelled("Force stopped")
                        if chunk:
                            handle.write(chunk)
                            bytes_written += len(chunk)
        if bytes_written <= 0 or not part_path.exists() or part_path.stat().st_size <= 0:
            raise DownloadError(
                "DOWNLOAD_EMPTY",
                "MP4 download failed: downloaded file was empty.",
                download_diagnostic(url, path, status_code, content_type, content_length, bytes_written, False),
            )
        part_path.replace(path)
        if not path.exists():
            raise DownloadError(
                "DOWNLOAD_FILE_MISSING",
                "MP4 download failed: raw file was not created.",
                download_diagnostic(url, path, status_code, content_type, content_length, bytes_written, False),
            )
        return download_diagnostic(url, path, status_code, content_type, content_length, bytes_written, True)
    except JobCancelled:
        part_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
    except DownloadError:
        part_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part_path.unlink(missing_ok=True)
        raise DownloadError(
            "DOWNLOAD_FAILED",
            f"MP4 download failed: {exc}",
            download_diagnostic(url, path, status_code, content_type, content_length, bytes_written, path.exists()),
        ) from exc


def download_diagnostic(url: str, path: Path, status_code: int, content_type: str, content_length: str, bytes_written: int, exists: bool) -> dict:
    parsed = urlparse(url)
    return {
        "status_code": status_code,
        "content_type": content_type,
        "content_length": content_length,
        "bytes_written": bytes_written,
        "url_host": parsed.netloc,
        "target_raw_path": str(path),
        "raw_exists_after_download": exists,
    }


def ensure_video_file(path: Path, code: str) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise DownloadError(code, f"MP4 save failed: expected file is missing or empty: {path}", {"path": str(path), "exists": path.exists()})


def save_downloaded_video(
    raw_path: Path,
    final_path: Path,
    save_mode: str,
    clean_watermark_enabled: bool,
    log_fn: Callable[[str, str], None],
    mark_action: Callable[[str], None],
) -> Path:
    ensure_video_file(raw_path, "DOWNLOAD_FILE_MISSING")
    artifact_path = raw_path
    if save_mode == "raw":
        final_path.unlink(missing_ok=True)
        log_fn(f"Saved raw video only: {raw_path.name}", "success")
        return raw_path
    if clean_watermark_enabled:
        mark_action("Cleaning watermark")
        if clean_video(raw_path, final_path):
            ensure_video_file(final_path, "FINAL_FILE_MISSING")
            artifact_path = final_path
            log_fn(f"Saved final video: {final_path.name}", "success")
            if save_mode != "both":
                raw_path.unlink(missing_ok=True)
            return artifact_path
        if raw_path.exists() and raw_path.stat().st_size > 0:
            log_fn("Watermark cleanup failed; using raw video as artifact.", "warn")
            return raw_path
        if final_path.exists() and final_path.stat().st_size > 0:
            log_fn("Watermark cleanup removed raw file; using final video artifact.", "warn")
            return final_path
        raise DownloadError("DOWNLOAD_FILE_MISSING", "MP4 download failed: raw file was not created.", {"raw_path": str(raw_path), "final_path": str(final_path)})
    shutil.copyfile(raw_path, final_path)
    ensure_video_file(final_path, "FINAL_FILE_MISSING")
    artifact_path = final_path
    log_fn(f"Saved final video: {final_path.name}", "success")
    if save_mode != "both":
        raw_path.unlink(missing_ok=True)
    return artifact_path


def unique_video_filename(output_dir: Path, vid: str, title: str = "") -> str:
    base = safe_filename(f"{vid}-{title}" if title else vid)
    stem = base.removesuffix(".mp4")
    candidate = base
    counter = 2
    while (output_dir / candidate).exists() or (output_dir / candidate.replace(".mp4", "_raw.mp4")).exists():
        candidate = f"{stem}-{counter}.mp4"
        counter += 1
    return candidate


def main() -> None:
    init_db()
    Worker(["auto-dola"], connection=Redis.from_url(settings.redis_url)).work()


if __name__ == "__main__":
    main()

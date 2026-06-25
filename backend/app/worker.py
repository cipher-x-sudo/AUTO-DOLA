from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Callable
from uuid import UUID

import httpx
from redis import Redis
from rq import Worker
from sqlmodel import Session, select

from app.config import settings
from app.database import engine, init_db
from app.models import Artifact, ItemStatus, Job, JobItem, JobKind, JobStatus, utcnow
from app.services.cookie_snapshots import append_raw_response_event, create_cookie_snapshot, mark_cookie_snapshot_conversation
from app.services.dola import DolaClient, DolaSubmissionError, DolaTerminalGenerationError, VIDEO_POLL_ATTEMPTS, build_dola_payload, format_diagnostic, is_terminal_video_failure
from app.services.dola_browser import DolaBrowserClient, DolaBrowserError, format_browser_diagnostic
from app.services.images import generate_image
from app.services.jobs import add_artifact, log, mark_item, recompute_job
from app.services.media import clean_video, safe_filename
from app.services.settings import load_public_settings
from app.services.tts import synthesize

logger = logging.getLogger(__name__)
MAX_DOLA_PARALLEL = 50
BROWSER_SUBMIT_PARALLEL = 5
HIGH_DEMAND_MIN_RETRIES = 8
HIGH_DEMAND_BACKOFF_SECONDS = (30, 45, 60, 90, 120, 150, 180)
BROWSER_FALLBACK_ERROR_CODES = {710022002, 710022017}


class JobCancelled(RuntimeError):
    """Raised inside a worker item when the user force-stops a job."""


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
    app_settings = load_public_settings(session)
    config = job.config_json
    output_dir = Path(config.get("save_folder") or app_settings.get("output_dir") or settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    proxy_url = app_settings.get("proxy_url", "") if app_settings.get("proxy_enabled") else ""
    dola_mode = str(app_settings.get("dola_mode") or settings.dola_mode or "hybrid").lower()
    if dola_mode not in {"direct", "browser", "hybrid"}:
        dola_mode = "hybrid"
    requested_duration = int(config.get("duration", 10))
    effective_dola_mode = "browser" if dola_mode == "hybrid" and requested_duration == 15 else dola_mode
    client = DolaClient(app_settings.get("dola_auth_cookies", settings.dola_auth_cookies), settings.dola_default_region, proxy=proxy_url)
    browser_client = DolaBrowserClient(proxy_url=proxy_url)
    items = session.exec(select(JobItem).where(JobItem.job_id == job.id)).all()
    item_numbers = {item.id: index + 1 for index, item in enumerate(items)}
    requested_parallel = config.get("parallel", 1)
    parallel = effective_video_parallel(requested_parallel)
    log(session, f"Video concurrency requested: {requested_parallel}", "info", job.id)
    log(session, f"Video concurrency effective: {parallel}", "info", job.id)
    log(session, f"Video settings: duration={requested_duration}, ratio={config.get('ratio', '9:16')}, mode={effective_dola_mode}, proxy_enabled={bool(proxy_url)}", "info", job.id)
    max_retries = max(int(config.get("max_retries", 3)), HIGH_DEMAND_MIN_RETRIES)
    semaphore = asyncio.Semaphore(parallel)
    browser_submit_semaphore = asyncio.Semaphore(min(BROWSER_SUBMIT_PARALLEL, parallel))

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
                    save_mode = str(config.get("save_mode") or "final").lower()
                    mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Downloading MP4")
                    await download_file(download_url, raw_path, cancel_fn=job_cancelled_or_missing)
                    ensure_not_cancelled(session_local, db_item.id)
                    artifact_path = raw_path
                    if save_mode == "raw":
                        final_path.unlink(missing_ok=True)
                        item_log(f"Saved raw video only: {raw_path.name}", "success")
                    elif config.get("clean_watermark", True):
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Cleaning watermark")
                        if clean_video(raw_path, final_path):
                            ensure_not_cancelled(session_local, db_item.id)
                            artifact_path = final_path
                            if save_mode != "both":
                                raw_path.unlink(missing_ok=True)
                        else:
                            artifact_path = raw_path
                            item_log("Watermark cleanup failed; using raw video as artifact.", "warn")
                    else:
                        shutil.copyfile(raw_path, final_path)
                        artifact_path = final_path
                        if save_mode != "both":
                            raw_path.unlink(missing_ok=True)
                    artifact = Artifact(job_id=job.id, item_id=db_item.id, kind="video", path=str(artifact_path), filename=artifact_path.name, mime_type="video/mp4", size_bytes=artifact_path.stat().st_size)
                    add_artifact(session_local, artifact, db_item)
                    mark_item(session_local, db_item, ItemStatus.completed, artifact_path.name)
                    item_log(f"Completed video: {artifact_path.name}", "success")

                async def run_browser_generation(action_prefix: str) -> None:
                    ensure_not_cancelled(session_local, db_item.id)
                    mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Submitting through Dola browser")
                    item_log("Connected to Dola browser fallback.", "info")
                    browser_slot_id = ""
                    browser_completed = False

                    async def cleanup_browser_slot(reason: str) -> None:
                        if not browser_slot_id:
                            return
                        item_log(f"Closing browser slot after {reason}: {browser_slot_id}", "info")
                        item_log("Deleting browser profile after rejection" if reason != "success" else "Deleting browser profile after success", "info")
                        if await browser_client.close_slot(browser_slot_id):
                            item_log("Browser profile cleaned", "success")
                        else:
                            item_log(f"Browser cleanup failed: {browser_slot_id}", "error")

                    async with browser_submit_semaphore:
                        browser_result = await browser_client.submit_and_capture_session(
                            db_item.prompt,
                            int(config.get("duration", 10)),
                            str(config.get("ratio", "9:16")),
                            log_fn=lambda message, level: item_log(f"Dola browser: {message}", level),
                        )
                    browser_slot_id = browser_result.slot_id
                    conversation_hint = browser_result.conversation_id[-8:] if len(browser_result.conversation_id) > 8 else browser_result.conversation_id
                    try:
                        item_log(f"Browser submitted Dola prompt: conversation_id=*{conversation_hint}, conversation_type={browser_result.conversation_type}.", "success")
                        item_log("Captured browser session. Switching to direct HTTP poll/download.", "info")
                        item_log(format_browser_diagnostic(browser_result.diagnostic), "info")
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Polling video id 1/{VIDEO_POLL_ATTEMPTS}")
                        item_log("Polling video id with browser session over HTTP.", "info")
                        vid = await client.poll_video_id(
                            browser_result.session,
                            browser_result.conversation_id,
                            browser_result.conversation_type,
                            max_attempts=VIDEO_POLL_ATTEMPTS,
                            log_fn=lambda message, level: progress_log()(f"Dola browser session: {message}" if not message.startswith("Polling ") else message, level),
                            cancel_fn=job_cancelled_or_missing,
                        )
                        if not vid:
                            item_log("Browser-session HTTP poll did not return video id. Capturing play_info from ready browser card.", "warn")
                            browser_download = await browser_client.wait_for_download_from_ready_card(
                                browser_result.conversation_id,
                                slot_id=browser_result.slot_id,
                                log_fn=lambda message, level: item_log(f"Dola browser: {message}", level),
                            )
                            item_log(format_browser_diagnostic(browser_download.diagnostic), "info")
                            await save_video_artifact(browser_download.download_url, browser_download.vid, action_prefix)
                            browser_completed = True
                            return
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Polling play_info 1/200")
                        item_log("Polling play_info with browser session over HTTP.", "info")
                        download_url = await client.poll_download_url(
                            browser_result.session,
                            vid,
                            cancel_fn=job_cancelled_or_missing,
                            log_fn=lambda message, level: progress_log()(f"Dola browser session: {message}" if not message.startswith("Polling ") else message, level),
                        )
                        if not download_url:
                            raise RuntimeError("Dola browser session did not return play_info URL.")
                        await save_video_artifact(download_url, vid, action_prefix)
                        browser_completed = True
                    finally:
                        await cleanup_browser_slot("success" if browser_completed else "rejection")

                for attempt in range(1, max_retries + 1):
                    try:
                        ensure_not_cancelled(session_local, db_item.id)
                        action_prefix = f"[{attempt}/{max_retries}] " if max_retries > 1 else ""
                        if effective_dola_mode == "browser":
                            await run_browser_generation(action_prefix)
                            return
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Building Dola session")
                        dola_session = await client.build_session()
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
                        submit_result = await client.submit(dola_session, payload, raw_response_fn=record_raw_response, attempt=attempt)
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
                        vid = await client.poll_video_id(
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
                                await run_browser_generation(action_prefix)
                                return
                            raise RuntimeError("Dola did not return video id.")
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Polling download URL")
                        download_url = await client.poll_download_url(
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
                                await run_browser_generation(action_prefix)
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
                                await run_browser_generation(action_prefix)
                                return
                            except DolaBrowserError as browser_exc:
                                last_error = str(browser_exc)
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
                        item_log(format_browser_diagnostic(exc.diagnostic), "error")
                        if attempt < max_retries:
                            item_log(f"Attempt {attempt} failed in Dola browser: {exc}. Retrying...", "warn")
                            if not await wait_before_retry(10):
                                return
                    except Exception as exc:
                        last_error = str(exc)
                        if attempt < max_retries:
                            item_log(f"Attempt {attempt} failed for '{db_item.prompt[:40]}': {exc}. Retrying...", "warn")
                            if not await wait_before_retry(3):
                                return
                mark_item(session_local, db_item, ItemStatus.failed, "Failed", last_error)
                item_log(f"Item failed after {max_retries} attempts: {last_error}", "error")
            finally:
                session_local.close()

    try:
        await asyncio.gather(*[_run_item(item) for item in items])
    finally:
        await client.aclose()
        await browser_client.close()


async def process_images(session: Session, job: Job) -> None:
    app_settings = load_public_settings(session)
    config = job.config_json
    output_dir = Path(config.get("output_folder") or app_settings.get("output_dir") or settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in session.exec(select(JobItem).where(JobItem.job_id == job.id)).all():
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
    for item in session.exec(select(JobItem).where(JobItem.job_id == job.id)).all():
        try:
            mark_item(session, item, ItemStatus.running, "Synthesizing speech")
            path = output_dir / safe_filename(f"tts-{item.id.hex[:8]}", ".mp3")
            await synthesize(item.prompt, config.get("voice", settings.tts_default_voice), path)
            artifact = Artifact(job_id=job.id, item_id=item.id, kind="audio", path=str(path), filename=path.name, mime_type="audio/mpeg", size_bytes=path.stat().st_size)
            add_artifact(session, artifact, item)
            mark_item(session, item, ItemStatus.completed, path.name)
        except Exception as exc:
            mark_item(session, item, ItemStatus.failed, "Failed", str(exc))


async def download_file(url: str, path: Path, cancel_fn: Callable[[], bool] | None = None) -> None:
    async with httpx.AsyncClient(timeout=60, verify=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    if cancel_fn and cancel_fn():
                        path.unlink(missing_ok=True)
                        raise JobCancelled("Force stopped")
                    if chunk:
                        handle.write(chunk)


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

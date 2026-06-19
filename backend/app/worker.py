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
from app.services.dola import DolaClient, DolaSubmissionError, DolaTerminalGenerationError, build_dola_payload, format_diagnostic, is_terminal_video_failure
from app.services.images import generate_image
from app.services.jobs import add_artifact, log, mark_item, recompute_job
from app.services.media import clean_video, safe_filename
from app.services.settings import load_public_settings
from app.services.tts import synthesize

logger = logging.getLogger(__name__)


class JobCancelled(RuntimeError):
    """Raised inside a worker item when the user force-stops a job."""


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
    client = DolaClient(app_settings.get("dola_auth_cookies", settings.dola_auth_cookies), settings.dola_default_region)
    items = session.exec(select(JobItem).where(JobItem.job_id == job.id)).all()
    item_numbers = {item.id: index + 1 for index, item in enumerate(items)}
    parallel = max(1, int(config.get("parallel", 5)))
    max_retries = config.get("max_retries", 3)
    semaphore = asyncio.Semaphore(parallel)

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

                def job_cancelled_or_missing() -> bool:
                    current_job = session_local.get(Job, job.id)
                    return current_job is None or current_job.status == JobStatus.cancelled

                for attempt in range(1, max_retries + 1):
                    try:
                        ensure_not_cancelled(session_local, db_item.id)
                        action_prefix = f"[{attempt}/{max_retries}] " if max_retries > 1 else ""
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
                        payload = build_dola_payload(dola_session.payload_template, db_item.prompt, config.get("duration", 15), config.get("ratio", "9:16"))
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
                            log_fn=lambda message, level: item_log(f"Dola: {message}", level),
                            raw_response_fn=record_raw_response,
                            cancel_fn=job_cancelled_or_missing,
                        )
                        if job_cancelled_or_missing():
                            current_item = session_local.get(JobItem, db_item.id)
                            if current_item:
                                mark_item(session_local, current_item, ItemStatus.cancelled, "Force stopped")
                            return
                        if not vid:
                            raise RuntimeError("Timed out waiting for Dola video id.")
                        mark_item(session_local, db_item, ItemStatus.running, f"{action_prefix}Polling download URL")
                        download_url = await client.poll_download_url(
                            dola_session,
                            vid,
                            raw_response_fn=record_raw_response,
                            cancel_fn=job_cancelled_or_missing,
                            log_fn=lambda message, level: item_log(f"Dola: {message}", level),
                        )
                        ensure_not_cancelled(session_local, db_item.id)
                        if not download_url:
                            raise RuntimeError("Timed out waiting for video download URL.")
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
                                shutil.copyfile(raw_path, final_path)
                                artifact_path = raw_path
                                final_path.unlink(missing_ok=True)
                                item_log("Watermark cleanup failed; kept raw video instead.", "warn")
                        else:
                            shutil.copyfile(raw_path, final_path)
                            artifact_path = final_path
                            if save_mode != "both":
                                raw_path.unlink(missing_ok=True)
                        artifact = Artifact(job_id=job.id, item_id=db_item.id, kind="video", path=str(artifact_path), filename=artifact_path.name, mime_type="video/mp4", size_bytes=artifact_path.stat().st_size)
                        add_artifact(session_local, artifact, db_item)
                        mark_item(session_local, db_item, ItemStatus.completed, artifact_path.name)
                        item_log(f"Completed video: {artifact_path.name}", "success")
                        return
                    except JobCancelled:
                        current_item = session_local.get(JobItem, db_item.id)
                        if current_item and current_item.status != ItemStatus.cancelled:
                            mark_item(session_local, current_item, ItemStatus.cancelled, "Force stopped")
                        item_log("Force stopped. Worker stopped processing this item.", "warn")
                        return
                    except DolaSubmissionError as exc:
                        last_error = str(exc)
                        item_log(format_diagnostic(exc.diagnostic), "error")
                        if attempt < max_retries:
                            item_log(f"Attempt {attempt} failed for '{db_item.prompt[:40]}': {exc}. Retrying...", "warn")
                            for _ in range(6):
                                if job_cancelled_or_missing():
                                    mark_item(session_local, db_item, ItemStatus.cancelled, "Force stopped")
                                    item_log("Force stopped. Worker stopped processing this item.", "warn")
                                    return
                                await asyncio.sleep(0.5)
                    except DolaTerminalGenerationError as exc:
                        last_error = str(exc)
                        mark_item(session_local, db_item, ItemStatus.failed, "Prompt flagged", last_error)
                        item_log(last_error, "error")
                        return
                    except Exception as exc:
                        last_error = str(exc)
                        if attempt < max_retries:
                            item_log(f"Attempt {attempt} failed for '{db_item.prompt[:40]}': {exc}. Retrying...", "warn")
                            for _ in range(6):
                                if job_cancelled_or_missing():
                                    mark_item(session_local, db_item, ItemStatus.cancelled, "Force stopped")
                                    item_log("Force stopped. Worker stopped processing this item.", "warn")
                                    return
                                await asyncio.sleep(0.5)
                mark_item(session_local, db_item, ItemStatus.failed, "Failed", last_error)
                item_log(f"Item failed after {max_retries} attempts: {last_error}", "error")
            finally:
                session_local.close()

    await asyncio.gather(*[_run_item(item) for item in items])


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

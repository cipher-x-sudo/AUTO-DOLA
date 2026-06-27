from datetime import timedelta
from pathlib import Path

from app.models import Job, JobItem, JobKind, JobStatus, utcnow
from app.routers.jobs import prepare_video_job_config, safe_output_folder_name, stable_job
from app.schemas import PromptItem, VideoJobCreate


def test_stable_job_sorts_items_by_created_at_then_id() -> None:
    now = utcnow()
    job = Job(kind=JobKind.video, status=JobStatus.queued, title="Video batch")
    first = JobItem(job_id=job.id, prompt="first", created_at=now)
    second = JobItem(job_id=job.id, prompt="second", created_at=now + timedelta(seconds=1))
    third = JobItem(job_id=job.id, prompt="third", created_at=now + timedelta(seconds=2))
    job.items = [third, first, second]

    stable = stable_job(job)

    assert [item.prompt for item in stable.items] == ["first", "second", "third"]


def test_safe_output_folder_name_uses_prompt_count_and_timestamp() -> None:
    assert safe_output_folder_name(5, "20260628-235959") == "5-prompts-20260628-235959"


def test_prepare_video_job_config_creates_per_generation_folder(tmp_path: Path) -> None:
    payload = VideoJobCreate(
        prompts=[PromptItem(prompt="one"), PromptItem(prompt="two")],
        save_folder=str(tmp_path),
    )

    config = prepare_video_job_config(payload)

    assert config["base_save_folder"] == str(tmp_path)
    assert config["job_output_folder"].startswith(str(tmp_path))
    assert config["job_output_folder_name"].startswith("2-prompts-")
    assert Path(config["job_output_folder"]).is_dir()

from datetime import timedelta

from app.models import Job, JobItem, JobKind, JobStatus, utcnow
from app.routers.jobs import stable_job


def test_stable_job_sorts_items_by_created_at_then_id() -> None:
    now = utcnow()
    job = Job(kind=JobKind.video, status=JobStatus.queued, title="Video batch")
    first = JobItem(job_id=job.id, prompt="first", created_at=now)
    second = JobItem(job_id=job.id, prompt="second", created_at=now + timedelta(seconds=1))
    third = JobItem(job_id=job.id, prompt="third", created_at=now + timedelta(seconds=2))
    job.items = [third, first, second]

    stable = stable_job(job)

    assert [item.prompt for item in stable.items] == ["first", "second", "third"]

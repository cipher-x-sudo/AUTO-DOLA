from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from app.models import ItemStatus, Job, JobItem, JobKind, JobStatus
from app.services.jobs import mark_item, recompute_job


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_recompute_keeps_cancelled_job_cancelled_when_all_items_cancelled() -> None:
    with make_session() as session:
        job = Job(kind=JobKind.video, status=JobStatus.cancelled, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)
        item = JobItem(job_id=job.id, prompt="test", status=ItemStatus.running)
        session.add(item)
        session.commit()

        mark_item(session, item, ItemStatus.cancelled, "Force stopped")
        updated = recompute_job(session, job.id)

        assert updated.status == JobStatus.cancelled
        assert updated.done == 0
        assert updated.failed == 0


def test_recompute_sets_all_cancelled_running_job_to_cancelled() -> None:
    with make_session() as session:
        job = Job(kind=JobKind.video, status=JobStatus.running, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)
        item = JobItem(job_id=job.id, prompt="test", status=ItemStatus.queued)
        session.add(item)
        session.commit()

        mark_item(session, item, ItemStatus.cancelled, "Force stopped")
        updated = recompute_job(session, job.id)

        assert updated.status == JobStatus.cancelled

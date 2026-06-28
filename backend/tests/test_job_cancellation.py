from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from app.models import Artifact, ItemStatus, Job, JobItem, JobKind, JobStatus
from app.routers import jobs as jobs_router
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


def test_force_stop_item_marks_only_that_item_cancelled() -> None:
    with make_session() as session:
        job = Job(kind=JobKind.video, status=JobStatus.running, title="Video batch (2)")
        session.add(job)
        session.commit()
        session.refresh(job)
        first = JobItem(job_id=job.id, prompt="one", status=ItemStatus.running)
        second = JobItem(job_id=job.id, prompt="two", status=ItemStatus.running)
        session.add(first)
        session.add(second)
        session.commit()
        session.refresh(first)
        session.refresh(second)

        result = jobs_router.force_stop_item(job.id, first.id, session)
        session.refresh(first)
        session.refresh(second)

        assert result == {"ok": True, "stopped": True}
        assert first.status == ItemStatus.cancelled
        assert first.action == "Force stopped"
        assert second.status == ItemStatus.running


def test_restart_completed_item_keeps_existing_artifact_and_queues_single_worker(monkeypatch) -> None:
    queued: list[tuple[str, tuple[str, str]]] = []

    class FakeQueue:
        def enqueue(self, name: str, *args: str, **_kwargs: object) -> None:
            queued.append((name, args))

    monkeypatch.setattr(jobs_router, "get_queue", lambda: FakeQueue())
    monkeypatch.setattr(jobs_router.settings, "auto_dola_inline_worker", False)

    with make_session() as session:
        job = Job(kind=JobKind.video, status=JobStatus.completed, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)
        item = JobItem(job_id=job.id, prompt="one", status=ItemStatus.completed, action="done")
        session.add(item)
        session.commit()
        session.refresh(item)
        artifact = Artifact(job_id=job.id, item_id=item.id, kind="video", path="/tmp/old.mp4", filename="old.mp4", mime_type="video/mp4", size_bytes=10)
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        item.artifact_id = artifact.id
        item.error = "old"
        item.diagnostic_json = {"error_type": "old"}
        session.add(item)
        session.commit()

        result = jobs_router.restart_item(job.id, item.id, session)
        session.refresh(job)
        session.refresh(item)

        assert result == {"ok": True, "queued": True}
        assert item.status == ItemStatus.queued
        assert item.action == "Restart queued"
        assert item.error is None
        assert item.diagnostic_json == {}
        assert item.artifact_id == artifact.id
        assert job.status == JobStatus.queued
        assert queued == [("app.worker.process_video_item", (str(job.id), str(item.id)))]

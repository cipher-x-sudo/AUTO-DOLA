from __future__ import annotations

from uuid import UUID

from redis import Redis
from rq import Queue

from app.config import settings


def get_queue() -> Queue:
    return Queue("auto-dola", connection=Redis.from_url(settings.redis_url))


def enqueue_job(kind: str, job_id: UUID) -> None:
    if settings.auto_dola_inline_worker:
        from app.worker import process_job

        process_job(str(job_id))
        return
    get_queue().enqueue("app.worker.process_job", str(job_id), job_timeout="6h", result_ttl=86400)

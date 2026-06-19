from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JobKind(StrEnum):
    video = "video"
    image = "image"
    tts = "tts"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ItemStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Job(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    kind: str = Field(index=True, max_length=32)
    status: str = Field(index=True, max_length=32, default=JobStatus.queued)
    title: str = Field(max_length=200)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    total: int = 0
    done: int = 0
    failed: int = 0
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON().with_variant(JSONB, "postgresql")))
    dola_cookie_snapshots_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list))
    error: str | None = None
    items: list["JobItem"] = Relationship(back_populates="job")
    artifacts: list["Artifact"] = Relationship(back_populates="job")


class JobItem(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="job.id", index=True)
    prompt: str
    title: str = Field(default="", max_length=240)
    status: str = Field(default=ItemStatus.queued, max_length=32)
    action: str = Field(default="", max_length=240)
    error: str | None = None
    artifact_id: UUID | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    job: Job | None = Relationship(back_populates="items")


class Artifact(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="job.id", index=True)
    item_id: UUID | None = Field(default=None, index=True)
    kind: str = Field(max_length=32)
    path: str
    filename: str = Field(max_length=260)
    mime_type: str = Field(max_length=120)
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    job: Job | None = Relationship(back_populates="artifacts")


class LogEvent(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID | None = Field(default=None, index=True)
    level: str = Field(max_length=20, default="info")
    message: str
    created_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=120)
    value_encrypted: str
    updated_at: datetime = Field(default_factory=utcnow)

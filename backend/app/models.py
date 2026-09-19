from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, UniqueConstraint
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
    paused = "paused"
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
    diagnostic_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict))
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


class DolaCookieProfile(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=160, index=True)
    cookies_encrypted: str
    cookie_names_json: list[str] = Field(default_factory=list, sa_column=Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list))
    daily_limit: int = Field(default=3, ge=1)
    enabled: bool = True
    validation_status: str = Field(default="pending", max_length=32)
    validation_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    deleted_at: datetime | None = None


class DolaCookieUsage(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("profile_id", "usage_day", name="uq_dola_cookie_usage_profile_day"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    profile_id: UUID = Field(index=True)
    usage_day: str = Field(max_length=10, index=True)
    completed_count: int = 0
    reserved_count: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class DolaCookieJobSnapshot(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="job.id", index=True)
    profile_id: UUID = Field(index=True)
    profile_name: str = Field(max_length=160)
    priority: int = 0
    daily_limit: int = 3
    cookies_encrypted: str
    cookie_names_json: list[str] = Field(default_factory=list, sa_column=Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list))
    created_at: datetime = Field(default_factory=utcnow)

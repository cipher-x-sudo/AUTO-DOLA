from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PromptItem(BaseModel):
    prompt: str = Field(min_length=1)
    title: str = ""


class VideoJobCreate(BaseModel):
    prompts: list[PromptItem] = Field(min_length=1)
    ratio: str = "9:16"
    duration: Literal[5, 10, 15] = 10
    save_folder: str = ""
    parallel: int = Field(default=5, ge=1)
    clean_watermark: bool = True
    save_mode: str = "final"


class ImageJobCreate(BaseModel):
    prompts: list[str] = Field(min_length=1)
    aspect_ratio: str = "1:1"
    output_folder: str = ""


class TTSJobCreate(BaseModel):
    lines: list[str] = Field(min_length=1)
    voice: str = "en-US-AriaNeural"
    output_folder: str = ""


class JobItemRead(BaseModel):
    id: UUID
    prompt: str
    title: str
    status: str
    action: str
    error: str | None
    diagnostic_json: dict[str, Any] = {}
    artifact_id: UUID | None
    updated_at: datetime


class ArtifactRead(BaseModel):
    id: UUID
    kind: str
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime


class JobRead(BaseModel):
    id: UUID
    kind: str
    status: str
    title: str
    created_at: datetime
    updated_at: datetime
    total: int
    done: int
    failed: int
    config_json: dict[str, Any]
    dola_cookie_snapshots_json: list[dict[str, Any]] = []
    error: str | None
    items: list[JobItemRead] = []
    artifacts: list[ArtifactRead] = []


class SettingsPayload(BaseModel):
    dola_auth_cookies: str = ""
    yousmind_api_key: str = ""
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash"
    default_ratio: str = "9:16"
    default_duration: int = 10
    default_parallel: int = 5
    output_dir: str = ""
    proxy_enabled: bool = False
    proxy_url: str = ""
    vpn_enabled: bool = False
    vpn_usernames: str = ""
    vpn_password: str = ""
    vpn_password_saved: bool = False
    vpn_browser_slots: int = 5
    browser_headless: bool = False
    tts_default_voice: str = "en-US-AriaNeural"
    dola_mode: Literal["direct", "browser", "hybrid"] = "hybrid"


class ProxyTestRequest(BaseModel):
    proxy_url: str


class VpnTestRequest(BaseModel):
    config_name: str = ""


class PromptGenerateRequest(BaseModel):
    master_prompt: str = Field(min_length=1)
    count: int = Field(default=5, ge=1)
    duration: Literal[5, 10, 15] = 10
    ratio: str = "9:16"
    style: str = "cinematic realistic"


class PromptGenerateResponse(BaseModel):
    prompts: list[str]
    model: str


class NicheRead(BaseModel):
    id: str
    name: str
    filename: str
    size_bytes: int


class NichePromptGenerateRequest(BaseModel):
    niche_ids: list[str] = Field(min_length=1)
    count: int = Field(default=5, ge=1)
    count_mode: str = "global"
    duration: Literal[5, 10, 15] = 10
    style: str = "cinematic realistic"
    existing_prompts: list[str] = []
    save: bool = True


class NichePromptGroup(BaseModel):
    niche_id: str
    niche_name: str
    filename: str
    requested_count: int
    prompts: list[str]
    saved_path: str


class NichePromptGenerateResponse(BaseModel):
    groups: list[NichePromptGroup]
    model: str


class NichePromptSaveRequest(BaseModel):
    niche_id: str = Field(min_length=1)
    prompts: list[str] = Field(min_length=1)


class NichePromptSaveResponse(BaseModel):
    saved_path: str


class HealthRead(BaseModel):
    ok: bool
    service: str
    environment: str
    database: str

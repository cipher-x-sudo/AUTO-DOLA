from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AUTO-DOLA"
    environment: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    public_api_base: str = "http://localhost:8000"
    database_url: str = "sqlite:///./autodola.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "change-me"
    settings_encryption_key: str = ""
    admin_token: str = "change-me"
    output_dir: Path = Path("./outputs")
    profile_dir: Path = Path("./profiles")
    log_dir: Path = Path("./logs")
    ffmpeg_bin: str = "ffmpeg"
    auto_dola_inline_worker: bool = False
    dola_auth_cookies: str = ""
    dola_default_region: str = "BD"
    yousmind_api_key: str = ""
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash"
    tts_default_voice: str = "en-US-AriaNeural"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def ensure_dirs(self) -> None:
        for path in (self.output_dir, self.profile_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    loaded = Settings()
    loaded.ensure_dirs()
    return loaded


settings = get_settings()

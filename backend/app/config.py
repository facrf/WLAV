from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://wlav:wlav@localhost:5432/wlav"
    media_root: Path = Path("/var/whatsapp_media")
    frontend_root: Path = PROJECT_ROOT / "frontend"
    app_host: str = "0.0.0.0"
    app_port: int = 21001
    log_level: str = "INFO"
    api_page_size: int = Field(default=50, ge=10, le=200)
    api_max_page_size: int = Field(default=200, ge=50, le=500)


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Application settings (env prefix HATTAMA_). No secrets have defaults usable in production."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


def _default_home() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Hattama"
    return Path("/var/lib/hattama")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HATTAMA_", env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore")

    env: Literal["dev", "prod", "test"] = "dev"
    database_url: str = ""
    data_dir: Path = Field(default_factory=lambda: _default_home() / "data")
    models_dir: Path = Field(default_factory=lambda: _default_home() / "models")
    tools_dir: Path = Field(default_factory=lambda: _default_home() / "tools")
    model_manifest: Path = REPO_ROOT / "model-configs" / "model-manifest.json"
    profiles_file: Path = REPO_ROOT / "model-configs" / "profiles.toml"
    profile: str = "gpu-6gb"

    # --- web / security
    public_origin: str = "http://localhost:5173"
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:8000"])
    allowed_extension_ids: list[str] = Field(default_factory=list)
    cookie_secure: bool = False
    session_ttl_hours: int = 12
    pairing_code_ttl_s: int = 300
    ingest_token_ttl_s: int = 6 * 3600
    agent_service_token_sha256: str = ""
    max_json_body_bytes: int = 1_000_000
    max_upload_bytes: int = 2 * 1024**3
    max_upload_duration_s: int = 4 * 3600
    login_rate_per_minute: int = 10

    # --- ingestion
    max_frame_bytes: int = 512 * 1024
    max_sources_per_session: int = 4
    reorder_window_frames: int = 64
    reorder_timeout_s: float = 2.0
    fsync_interval_s: float = 0.25
    stop_grace_s: float = 15.0

    # --- LLM (only administrator-configured internal endpoints)
    llm_base_url: str = "http://127.0.0.1:8081"
    llm_allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "llm"])
    llm_timeout_s: float = 300.0
    llm_max_retries: int = 1

    # --- time
    default_timezone: str = "Asia/Almaty"

    # --- retention (days); 0 = keep until manual deletion
    retention_audio_days: int = 90
    retention_transcript_days: int = 365

    @field_validator("database_url", mode="before")
    @classmethod
    def _default_db(cls, value: str) -> str:
        return value or ""

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'hattama.sqlite3').as_posix()}"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()

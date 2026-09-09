"""Application configuration.

Every value is overridable through environment variables or a local ``.env`` file.
Secrets are never written back to disk by this module.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Central settings object. Instantiate via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- storage
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    logs_dir: Path = Field(default=PROJECT_ROOT / "logs")
    screenshots_dir: Path = Field(default=PROJECT_ROOT / "screenshots")
    browser_profile_dir: Path = Field(default=PROJECT_ROOT / "data" / "browser-profile")
    database_url: str = Field(default="")

    # ------------------------------------------------------------------ search
    search_location: str = Field(default="Varna")
    search_keywords: list[str] = Field(default_factory=list)
    max_pages_per_scan: int = Field(default=5, ge=1, le=50)
    max_jobs_per_scan: int = Field(default=120, ge=1, le=1000)

    # ------------------------------------------------------------- decisioning
    auto_apply: bool = Field(default=False)
    auto_apply_threshold: int = Field(default=90, ge=0, le=100)
    review_threshold: int = Field(default=75, ge=0, le=100)
    max_seniority: Literal[
        "internship", "entry", "junior", "junior_mid", "mid", "mid_senior", "senior", "lead"
    ] = Field(default="mid")
    max_auto_applications_per_run: int = Field(default=5, ge=0, le=50)

    # ------------------------------------------------------------------ browser
    browser_headless: bool = Field(default=False)
    browser_channel: str | None = Field(default=None)
    browser_slow_mo_ms: int = Field(default=0, ge=0, le=5000)
    browser_timeout_ms: int = Field(default=30_000, ge=1000)
    browser_locale: str = Field(default="bg-BG")
    request_delay_seconds: float = Field(default=3.0, ge=0.5)
    request_jitter_seconds: float = Field(default=1.5, ge=0.0)
    max_retries: int = Field(default=3, ge=0, le=10)
    respect_robots_txt: bool = Field(default=True)

    # ----------------------------------------------------------------------- ai
    # The local model is the default matcher: the prompt carries the candidate's
    # CV, and a daily job hunt is exactly the workload a hosted API suits worst.
    ai_provider: Literal["local", "rule_based", "anthropic", "openai", "auto"] = Field(
        default="local"
    )
    local_model: str = Field(default="qwen2.5:3b")
    local_model_host: str = Field(default="http://127.0.0.1:11434")
    local_model_timeout_seconds: float = Field(default=180.0, ge=10.0)
    local_model_num_ctx: int = Field(default=6144, ge=1024, le=32768)
    local_model_num_predict: int = Field(default=1100, ge=200, le=4096)
    # Sending the CV to a local model is private; sending it anywhere else is not.
    send_cv_text_to_local_model: bool = Field(default=True)
    anthropic_api_key: SecretStr | None = Field(default=None)
    anthropic_model: str = Field(default="claude-sonnet-5")
    openai_api_key: SecretStr | None = Field(default=None)
    openai_model: str = Field(default="gpt-4o-mini")
    ai_timeout_seconds: float = Field(default=60.0, ge=5.0)
    ai_max_description_chars: int = Field(default=6000, ge=500)
    send_cv_text_to_ai: bool = Field(default=False)

    # ------------------------------------------------------------- cover letter
    cover_letter_enabled: bool = Field(default=True)
    cover_letter_max_words: int = Field(default=180, ge=50, le=600)

    # -------------------------------------------------------------- scheduling
    scheduler_enabled: bool = Field(default=False)
    scan_interval_hours: float = Field(default=24.0, ge=0.5)
    scan_at_hour: int = Field(default=9, ge=0, le=23)

    # ----------------------------------------------------------- notifications
    notify_console: bool = Field(default=True)
    notify_dashboard: bool = Field(default=True)
    telegram_bot_token: SecretStr | None = Field(default=None)
    telegram_chat_id: str | None = Field(default=None)

    # ------------------------------------------------------------------ server
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000, ge=1, le=65535)

    # ----------------------------------------------------------------- logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_json: bool = Field(default=False)

    @field_validator("search_keywords", mode="before")
    @classmethod
    def _split_keywords(cls, value: object) -> object:
        """Accept a comma-separated string from the environment."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _default_database_url(cls, value: str) -> str:
        return value or ""

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'jobhunter.db').as_posix()}"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def ensure_directories(self) -> None:
        """Create every directory the application writes to."""
        for path in (
            self.data_dir,
            self.logs_dir,
            self.screenshots_dir,
            self.browser_profile_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def thresholds_are_sane(self) -> bool:
        return self.auto_apply_threshold >= self.review_threshold


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    settings = Settings()
    settings.ensure_directories()
    return settings


def reset_settings_cache() -> None:
    """Clear the cached settings. Used by tests and the settings UI."""
    get_settings.cache_clear()

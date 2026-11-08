"""Application settings loaded from environment variables and `.env`."""

import sys
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lumen.timezones import kyiv_timezone


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _project_root()
DEFAULT_LUMEN_USER_ID = "VST"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    lumen_env: str = "development"
    lumen_user_id: str = DEFAULT_LUMEN_USER_ID
    log_level: str = "DEBUG" #DEBUG INFO WARNING ERROR CRITICAL
    log_file_path: str = "./data/debug/lumen.log"
    lumen_host: str = "0.0.0.0"
    lumen_port: int = 8010
    lumen_db_path: str = "./data/lumen.db"

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4:e4b"
    ollama_dialogue_model: str = ""
    ollama_embedding_model: str = "embeddinggemma"
    ollama_timeout_seconds: float = 240.0
    ollama_stream_idle_timeout_seconds: float = 240.0
    ollama_client_keep_alive: str = "5m"
    ollama_debug_dump_path: str = "./data/debug/last_ollama_request.json"
    ollama_compat_user_id: str = ""
    dialogue_memory_window_hours: int = 3
    conversation_recent_turn_limit: int = Field(default=8, ge=1, le=50)
    conversation_context_char_limit: int = Field(default=6000, ge=256)
    fact_semantic_min_similarity: float = Field(default=0.37, ge=0.0, le=1.0)
    fact_context_item_limit: int = Field(default=10, ge=1)
    fact_context_char_limit: int = Field(default=2400, ge=1)

    home_assistant_url: str = "http://homeassistant.local:8123"
    home_assistant_token: str = ""
    home_assistant_webhook_id: str = "lumen_hook_f8e9d2a1"
    home_assistant_webhook_url: str = ""
    home_assistant_calendar_entity_ids: str = ""
    home_assistant_calendar_write_entity_id: str = ""
    home_assistant_calendar_target_chat_id: str = ""
    home_assistant_calendar_all_day_time: str = "09:00"
    home_assistant_calendar_reconnect_seconds: float = 5.0
    home_assistant_calendar_default_duration_minutes: int = 60
    knowledge_paths: str = "./data/knowledge"
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = ""
    ffmpeg_path: str = "ffmpeg"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session_path: str = "./data/telegram/lumen"
    telegram_default_message_limit: int = 30
    telegram_channel_history_hours: int = 4
    telegram_dialog_history_hours: int = 48
    reminder_grace_minutes: int = 30
    reminder_snooze_minutes: int = 15
    reminder_management_session_ttl_minutes: int = 10
    nutrition_enabled: bool = True
    nutrition_directory: str = "./data/nutrition"
    nutrition_model: str = ""
    nutrition_timezone: str = "Europe/Kyiv"
    nutrition_image_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    nutrition_image_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("nutrition_timezone")
    @classmethod
    def _validate_nutrition_timezone(cls, value: str) -> str:
        value = value.strip()
        if value in {"Europe/Kyiv", "Europe/Kiev"}:
            kyiv_timezone()
            return "Europe/Kyiv"
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("NUTRITION_TIMEZONE must be an available IANA timezone") from exc
        return value

    @field_validator("lumen_user_id")
    @classmethod
    def _normalize_lumen_user_id(cls, value: str) -> str:
        return value.strip() or DEFAULT_LUMEN_USER_ID

    @field_validator("log_file_path", "lumen_db_path", "ollama_debug_dump_path", "telegram_session_path", "nutrition_directory", mode="after")
    @classmethod
    def _resolve_project_path(cls, value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    @field_validator("home_assistant_calendar_all_day_time")
    @classmethod
    def _validate_calendar_all_day_time(cls, value: str) -> str:
        parsed = time.fromisoformat(value)
        if parsed.second or parsed.microsecond:
            raise ValueError("HOME_ASSISTANT_CALENDAR_ALL_DAY_TIME must be precise to a minute")
        return value

    @field_validator("home_assistant_calendar_reconnect_seconds")
    @classmethod
    def _validate_calendar_reconnect_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("HOME_ASSISTANT_CALENDAR_RECONNECT_SECONDS must be greater than zero")
        return value

    @field_validator("ollama_stream_idle_timeout_seconds")
    @classmethod
    def _validate_ollama_stream_idle_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("OLLAMA_STREAM_IDLE_TIMEOUT_SECONDS must be greater than zero")
        return value

    @field_validator("home_assistant_calendar_default_duration_minutes")
    @classmethod
    def _validate_calendar_default_duration(cls, value: int) -> int:
        if not 1 <= value <= 1440:
            raise ValueError("HOME_ASSISTANT_CALENDAR_DEFAULT_DURATION_MINUTES must be between 1 and 1440")
        return value

    @field_validator(
        "reminder_grace_minutes",
        "reminder_snooze_minutes",
        "reminder_management_session_ttl_minutes",
    )
    @classmethod
    def _validate_positive_reminder_minutes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Reminder timing settings must be greater than zero")
        return value

    @property
    def knowledge_path_list(self) -> list[str]:
        return [item.strip() for item in self.knowledge_paths.split(";") if item.strip()]

    @property
    def home_assistant_calendar_entity_id_list(self) -> list[str]:
        return [item.strip() for item in self.home_assistant_calendar_entity_ids.split(";") if item.strip()]

    @property
    def home_assistant_calendar_all_day_notification_time(self) -> time:
        return time.fromisoformat(self.home_assistant_calendar_all_day_time)

"""Application settings loaded from environment variables and `.env`."""

import sys
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    lumen_env: str = "development"
    lumen_user_id: str = "VST"
    log_level: str = "DEBUG" #DEBUG INFO WARNING ERROR CRITICAL
    log_file_path: str = "./data/debug/lumen.log"
    lumen_host: str = "0.0.0.0"
    lumen_port: int = 8010
    lumen_db_path: str = "./data/lumen.db"

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4:e4b"
    ollama_dialogue_model: str = ""
    ollama_embedding_model: str = "embeddinggemma"
    ollama_timeout_seconds: float = 120.0
    ollama_client_keep_alive: str = "30m"
    ollama_debug_dump_path: str = "./data/debug/last_ollama_request.json"
    ollama_compat_user_id: str = ""
    dialogue_memory_window_hours: int = 3

    home_assistant_url: str = "http://homeassistant.local:8123"
    home_assistant_token: str = ""
    home_assistant_webhook_id: str = "lumen_hook_f8e9d2a1"
    home_assistant_webhook_url: str = ""
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

    @field_validator("log_file_path", "lumen_db_path", "ollama_debug_dump_path", "telegram_session_path", mode="after")
    @classmethod
    def _resolve_project_path(cls, value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    @property
    def knowledge_path_list(self) -> list[str]:
        return [item.strip() for item in self.knowledge_paths.split(";") if item.strip()]

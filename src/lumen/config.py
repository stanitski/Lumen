from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    lumen_env: str = "development"
    lumen_host: str = "127.0.0.1"
    lumen_port: int = 8010
    lumen_db_path: str = "./data/lumen.db"

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4:e4b"
    ollama_timeout_seconds: float = 120.0
    ollama_keep_alive: str = "30m"

    home_assistant_url: str = "http://homeassistant.local:8123"
    home_assistant_token: str = ""
    knowledge_paths: str = "./data/knowledge"

    @property
    def knowledge_path_list(self) -> list[str]:
        return [item.strip() for item in self.knowledge_paths.split(";") if item.strip()]

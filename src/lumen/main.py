from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from lumen.actions import ActionRegistry
from lumen.api.routes.admin import router as admin_router
from lumen.api.routes.assist import router as assist_router
from lumen.api.routes.chat import router as chat_router
from lumen.api.routes.health import router as health_router
from lumen.api.routes.home_assistant import router as home_assistant_router
from lumen.api.routes.knowledge import router as knowledge_router
from lumen.api.routes.memory import router as memory_router
from lumen.api.routes.ui import router as ui_router
from lumen.config import Settings
from lumen.connectors.home_assistant import HomeAssistantConnector
from lumen.connectors.ollama import OllamaConnector
from lumen.knowledge.store import KnowledgeStore
from lumen.memory.store import MemoryStore
from lumen.services.agent import AgentService
from lumen.services.bootstrap import BootstrapService
from lumen.storage.db import Database


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.lumen_db_path)
        self.database.init()
        self.memory_store = MemoryStore(self.database.session)
        self.knowledge_store = KnowledgeStore(self.database.session)
        self.home_assistant = HomeAssistantConnector(
            base_url=settings.home_assistant_url,
            token=settings.home_assistant_token,
        )
        self.ollama = OllamaConnector(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout_seconds=settings.ollama_timeout_seconds,
            keep_alive=settings.ollama_keep_alive,
        )
        self.action_registry = ActionRegistry()
        self.agent_service = AgentService(
            memory_store=self.memory_store,
            knowledge_store=self.knowledge_store,
            ollama=self.ollama,
            home_assistant=self.home_assistant,
            action_registry=self.action_registry,
            session_factory=self.database.session,
        )
        self.bootstrap_service = BootstrapService(
            knowledge_store=self.knowledge_store,
            home_assistant=self.home_assistant,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    app = FastAPI(title="Lumen Core", version="0.1.0")
    app.state.container = AppContainer(resolved_settings)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(ui_router)
    app.include_router(health_router)
    app.include_router(assist_router)
    app.include_router(chat_router)
    app.include_router(memory_router)
    app.include_router(knowledge_router)
    app.include_router(home_assistant_router)
    app.include_router(admin_router)
    return app


app = create_app()

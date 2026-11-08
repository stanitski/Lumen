"""Entry point invoked by the Windows one-shot reminder wake task."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.services.conversation import ConversationService
from lumen.services.delivery import DeliveryService
from lumen.services.memory import MemoryService
from lumen.services.reminder_wake import WindowsWakeAdapter
from lumen.services.reminders import ReminderService
from lumen.storage.db import Database


async def _run_with_index_cleanup(service: ReminderService, tasks: set[asyncio.Task[Any]]) -> None:
    try:
        await service.run_due()
    finally:
        pending = tuple(tasks)
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            logger = logging.getLogger("lumen")
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    logger.warning("Reminder wake indexing task failed error_type=%s", type(result).__name__)


def run(db_path: str | None = None) -> int:
    settings = Settings()
    database = Database(db_path or settings.lumen_db_path)
    database.init()
    logger = logging.getLogger("lumen")
    memory_store = MemoryStore(database.session)
    ollama = OllamaConnector(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
        keep_alive=settings.ollama_client_keep_alive,
        debug_dump_path=settings.ollama_debug_dump_path,
    )
    memory_service = MemoryService(memory_store, ollama, settings, default_user_id="global", logger=logger)
    delivery_service = DeliveryService(settings, logger=logger)
    indexing_tasks: set[asyncio.Task[Any]] = set()

    def start_indexing(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        indexing_tasks.add(task)
        return task

    conversation_service = ConversationService(
        memory_service, delivery_service, logger=logger, task_starter=start_indexing,
    )
    service = ReminderService(
        database.session,
        delivery_service,
        WindowsWakeAdapter(db_path=database.db_path),
        missed_grace_minutes=settings.reminder_grace_minutes,
        default_snooze_minutes=settings.reminder_snooze_minutes,
        management_session_minutes=settings.reminder_management_session_ttl_minutes,
        conversation_service=conversation_service,
    )
    asyncio.run(_run_with_index_cleanup(service, indexing_tasks))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path")
    args = parser.parse_args()
    raise SystemExit(run(args.db_path))

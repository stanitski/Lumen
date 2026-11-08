from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lumen.reminder_wakeup import run
from lumen.services.conversation import ConversationService


class FakeSettings:
    lumen_db_path = "unused.db"
    ollama_base_url = "http://127.0.0.1:11434"
    ollama_model = "test"
    ollama_timeout_seconds = 1
    ollama_client_keep_alive = "1m"
    ollama_debug_dump_path = ""
    reminder_grace_minutes = 30
    reminder_snooze_minutes = 15
    reminder_management_session_ttl_minutes = 10


class FakeDatabase:
    def __init__(self, path: str) -> None:
        self.db_path = path
        self.initialized = False

    def init(self) -> None:
        self.initialized = True

    def session(self) -> None:
        return None


class FakeMemoryService:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, object]] = {}
        self.index_started = asyncio.Event()
        self.index_cancelled = False

    def add_turn_once(self, *, user_id: str, event_id: str, **values: object) -> int:
        self.events.setdefault(event_id, {"user_id": user_id, **values})
        return 1

    def get_conversation_event(self, *, user_id: str, event_id: str) -> None:
        return None

    async def postprocess_dialogue_turn(self, *, user_id: str, turn_id: int) -> None:
        self.index_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.index_cancelled = True
            raise


class FakeReminderService:
    instance: "FakeReminderService | None" = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.conversation_service = kwargs["conversation_service"]
        self.delivery_service = args[1]
        self.recorded_turn_id: int | None = None
        FakeReminderService.instance = self

    async def run_due(self) -> int:
        self.recorded_turn_id = await self.conversation_service.record_event(
            user_id="u1",
            role="assistant",
            content="Випити воду",
            question="",
            kind="reminder_notification",
            event_id="reminder:delivery-1",
            metadata={"reminder_id": 7, "delivery_id": "delivery-1"},
        )
        await asyncio.sleep(0)
        return 1


class ReminderWakeHistoryTests(unittest.TestCase):
    def test_source_wakeup_wires_shared_history_and_cancels_background_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "isolated.db")
            memory = FakeMemoryService()
            database = FakeDatabase(db_path)
            delivery = object()
            wake = object()

            with (
                patch("lumen.reminder_wakeup.Settings", return_value=FakeSettings()),
                patch("lumen.reminder_wakeup.Database", return_value=database),
                patch("lumen.reminder_wakeup.MemoryStore", return_value=object()),
                patch("lumen.reminder_wakeup.OllamaConnector", return_value=object()),
                patch("lumen.reminder_wakeup.MemoryService", return_value=memory),
                patch("lumen.reminder_wakeup.DeliveryService", return_value=delivery),
                patch("lumen.reminder_wakeup.WindowsWakeAdapter", return_value=wake),
                patch("lumen.reminder_wakeup.ReminderService", side_effect=FakeReminderService),
            ):
                self.assertEqual(0, run(db_path))

        reminder = FakeReminderService.instance
        self.assertIsNotNone(reminder)
        assert reminder is not None
        self.assertIsInstance(reminder.conversation_service, ConversationService)
        self.assertIs(delivery, reminder.delivery_service)
        self.assertTrue(database.initialized)
        self.assertEqual(1, reminder.recorded_turn_id)
        self.assertEqual("Випити воду", memory.events["reminder:delivery-1"]["answer"])
        self.assertEqual("", memory.events["reminder:delivery-1"]["question"])
        self.assertTrue(memory.index_started.is_set())
        self.assertTrue(memory.index_cancelled)


if __name__ == "__main__":
    unittest.main()

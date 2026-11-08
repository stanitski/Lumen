from __future__ import annotations

import sys
import tempfile
import unittest
import json
import asyncio
from unittest.mock import Mock, patch
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.runtime_test_support import main
from lumen.models import ChatResult
from lumen.config import Settings
from lumen.memory.store import MemoryStore
from lumen.models import HomeAssistantCallbackPayload
from lumen.services.delivery import DeliveryService
from lumen.storage.db import Database


class FakeChatService:
    def __init__(self, *, execution_outcome: str = "ok") -> None:
        self.execution_outcome = execution_outcome

    async def process_chat_request(self, payload, request_id: str) -> None:
        del payload, request_id

    async def process_chat_request_with_context(self, payload, request_id: str, **kwargs) -> None:
        del payload, request_id, kwargs

    async def process_chat_request_result(self, payload, request_id: str, *, deliver: bool = True, **kwargs) -> ChatResult:
        del kwargs
        return ChatResult(
            request_id=request_id,
            user_id=payload.user_id or "VST",
            answer=f"answer:{payload.text}",
            delivered=deliver,
            execution_outcome=self.execution_outcome,
        )

    async def debug_memory_search(self, *, user_text: str, user_id: str) -> dict[str, object]:
        return {"answer": f"answer:{user_text}", "context": {"user_id": user_id}, "message_count": 2}


class OllamaCompatibleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = main.chat_service
        main.chat_service = FakeChatService()
        self.client = TestClient(main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))

    def tearDown(self) -> None:
        main.chat_service = self.original

    def test_health_and_memory_manager_exist(self) -> None:
        self.assertEqual(200, self.client.get("/").status_code)
        response = self.client.get("/memory")
        self.assertEqual(200, response.status_code)
        self.assertIn("Пам’ять Lumen", response.text)

    def test_ollama_chat_uses_chat_pipeline(self) -> None:
        response = self.client.post("/api/chat", json={"model": "lumen", "stream": False, "messages": [{"role": "user", "content": "Hello"}]})
        self.assertEqual(200, response.status_code)
        self.assertEqual("answer:Hello", response.json()["message"]["content"])

    def test_ollama_chat_stream_is_valid_ndjson(self) -> None:
        response = self.client.post("/api/chat", json={"model": "lumen", "stream": True, "messages": [{"role": "user", "content": "Hello"}]})

        self.assertEqual(200, response.status_code)
        event = json.loads(response.text)
        self.assertEqual("answer:Hello", event["message"]["content"])
        self.assertIs(event["done"], True)

    def test_ollama_idle_stream_is_reported_as_compatible_gateway_timeout(self) -> None:
        main.chat_service = FakeChatService(execution_outcome="cancelled_idle")

        for endpoint, payload in (
            ("/api/chat", {"model": "lumen", "stream": False, "messages": [{"role": "user", "content": "Hello"}]}),
            ("/api/generate", {"model": "lumen", "stream": True, "prompt": "Hello"}),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, json=payload)

                self.assertEqual(504, response.status_code)
                self.assertEqual({"error": "Ollama stream stalled"}, response.json())

    def test_debug_search_uses_chat_pipeline(self) -> None:
        response = self.client.post("/memory/debug-search", json={"text": "find this", "user_id": "VST"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("answer:find this", response.json()["answer"])

    def test_memory_defaults_follow_configured_user(self) -> None:
        with patch.object(main, "settings", Settings(lumen_user_id="  alternate-user  ")):
            with patch.object(main.memory_store, "list_facts", return_value=[]) as list_facts:
                response = self.client.get("/memory/items", params={"item_type": "facts"})
            self.assertEqual(200, response.status_code)
            list_facts.assert_called_once_with("alternate-user", limit=100)

            response = self.client.post("/memory/debug-search", json={"text": "find this"})
            self.assertEqual("alternate-user", response.json()["context"]["user_id"])
            self.assertNotIn("user_id:'VST'", self.client.get("/memory").text)

    def test_blank_configured_user_keeps_existing_default(self) -> None:
        self.assertEqual("VST", Settings(lumen_user_id="  ").lumen_user_id)

    def test_memory_turn_delete_respects_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "memory.db"))
            database.init()
            store = MemoryStore(database.session)
            turn_id = store.add_turn(user_id="owner", source="test", question="q", answer="a")

            with patch.object(main, "memory_store", store):
                response = self.client.post("/memory/delete", json={"type": "turn", "id": turn_id, "user_id": "other"})
                self.assertEqual({"deleted": 0}, response.json())
                self.assertEqual(1, len(store.list_turns(user_id="owner")))

                response = self.client.post("/memory/delete", json={"type": "turn", "id": turn_id, "user_id": "owner"})
                self.assertEqual({"deleted": 1}, response.json())
                self.assertEqual([], store.list_turns(user_id="owner"))

    def test_memory_delete_rejects_malformed_id(self) -> None:
        for item_id in (None, "missing", 0, -1, 1.5, True):
            with self.subTest(item_id=item_id):
                response = self.client.post("/memory/delete", json={"type": "turn", "id": item_id})
                self.assertEqual(400, response.status_code)

    def test_legacy_reminder_callback_is_rejected_explicitly(self) -> None:
        response = self.client.post("/connector", json={"source": "telegram", "event_type": "reminder_confirmed", "user_id": "VST", "data": {"reminder_id": "7"}})

        self.assertEqual(202, response.status_code)
        self.assertEqual("legacy_reminder_confirmation_missing_delivery_id", response.json()["reason"])

    def test_ha_reminder_contract_carries_delivery_id(self) -> None:
        reminder_id = 9
        delivery_id = "a" * 32
        payload = HomeAssistantCallbackPayload(
            request_id="request-reminder",
            kind="reminder",
            user_id="VST",
            answer="Reminder",
            voice=False,
            reminder_id=reminder_id,
            metadata={
                "target_channel": "telegram",
                "target_chat_id": "492198718",
                "reminder_id": reminder_id,
                "delivery_id": delivery_id,
            },
        )

        self.assertEqual("492198718", payload.metadata["target_chat_id"])
        callback_data = f"reminder_confirmed:{payload.reminder_id}:{payload.metadata['delivery_id']}"
        self.assertIn(str(reminder_id), callback_data)
        self.assertIn(delivery_id, callback_data)
        self.assertLessEqual(len(callback_data.encode("utf-8")), 64)


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_recovery_runs_in_background(self) -> None:
        class FakeReminderService:
            async def run_due(self) -> None:
                await asyncio.sleep(0)

        with patch.object(main, "reminder_service", FakeReminderService()), patch.object(main, "calendar_client", None):
            await main.start_reminder_recovery()
            task = main.app.state.reminder_recovery_task
            self.assertIsInstance(task, asyncio.Task)
            await task
            await asyncio.sleep(0)

    async def test_background_task_failure_is_logged_and_removed(self) -> None:
        async def fail() -> None:
            raise RuntimeError("test failure")

        with patch.object(main.logger, "exception") as log_exception:
            task = main._start_background_task(fail(), label="chat request=test-request")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertNotIn(task, main.app.state.background_tasks)
        log_exception.assert_called_once_with("Background task failed: %s", "chat request=test-request")

    async def test_shutdown_cancels_chat_and_reminder_tasks(self) -> None:
        class FakeReminderService:
            async def run_due(self) -> None:
                await asyncio.Event().wait()

        async def wait_for_chat() -> None:
            await asyncio.Event().wait()

        with patch.object(main, "reminder_service", FakeReminderService()), patch.object(main, "calendar_client", None):
            await main.start_reminder_recovery()
            reminder_task = main.app.state.reminder_recovery_task
            chat_task = main._start_background_task(wait_for_chat(), label="chat request=test-request")
            await asyncio.sleep(0)
            await main.stop_home_assistant_calendar()
            await asyncio.sleep(0)

        self.assertTrue(reminder_task.cancelled())
        self.assertTrue(chat_task.cancelled())
        self.assertFalse(main.app.state.background_tasks)


class DeliveryLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_logger_does_not_receive_route_or_full_metadata(self) -> None:
        class FakeResponse:
            is_success = True
            status_code = 200

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def post(self, *args, **kwargs):
                return FakeResponse()

        logger = Mock()
        service = DeliveryService(Settings(home_assistant_webhook_url="http://ha.local/webhook"), logger=logger)
        payload = HomeAssistantCallbackPayload(
            request_id="request-1",
            kind="reminder",
            user_id="VST",
            answer="Reminder",
            voice=False,
            reminder_id=7,
            metadata={"target_channel": "telegram", "target_chat_id": "492198718", "delivery_id": "a" * 32},
        )

        with patch("lumen.services.delivery.httpx.AsyncClient", return_value=FakeClient()):
            await service.post_home_assistant_callback(payload)

        logged = " ".join(str(call) for call in logger.debug.call_args_list)
        self.assertNotIn("target_chat_id", logged)
        self.assertNotIn("492198718", logged)
        self.assertNotIn("a" * 32, logged)


if __name__ == "__main__":
    unittest.main()

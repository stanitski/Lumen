from __future__ import annotations

import sys
import unittest
import json
import asyncio
from unittest.mock import Mock, patch
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import lumen.main as main
from lumen.models import ChatResult
from lumen.config import Settings
from lumen.models import HomeAssistantCallbackPayload
from lumen.services.delivery import DeliveryService


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
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.chat_service = self.original

    def test_health_and_memory_manager_exist(self) -> None:
        self.assertEqual(200, self.client.get("/").status_code)
        self.assertIn("Lumen memory", self.client.get("/memory").text)

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

        original = main.reminder_service
        main.reminder_service = FakeReminderService()
        try:
            await main.start_reminder_recovery()
            task = main.app.state.reminder_recovery_task
            self.assertIsInstance(task, asyncio.Task)
            await task
        finally:
            main.reminder_service = original


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

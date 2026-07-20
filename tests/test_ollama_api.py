from __future__ import annotations

import sys
import unittest
import json
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import lumen.main as main
from lumen.models import ChatResult


class FakeChatService:
    async def process_chat_request(self, payload, request_id: str) -> None:
        del payload, request_id

    async def process_chat_request_with_context(self, payload, request_id: str, **kwargs) -> None:
        del payload, request_id, kwargs

    async def process_chat_request_result(self, payload, request_id: str, *, deliver: bool = True, **kwargs) -> ChatResult:
        del kwargs
        return ChatResult(request_id=request_id, user_id=payload.user_id or "VST", answer=f"answer:{payload.text}", delivered=deliver)

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

    def test_debug_search_uses_chat_pipeline(self) -> None:
        response = self.client.post("/memory/debug-search", json={"text": "find this", "user_id": "VST"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("answer:find this", response.json()["answer"])


if __name__ == "__main__":
    unittest.main()

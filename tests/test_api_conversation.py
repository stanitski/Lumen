from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from tests.runtime_test_support import main
from lumen.models import ChatResult, KnowledgeUploadResult, NewsProcessingResult
from lumen.services.speech_to_text import SpeechToTextError


class ApiConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_knowledge_upload_records_result_without_file_body(self):
        result = KnowledgeUploadResult(status="indexed", kind="text", scope="personal",
            user_id="api-history", filename="notes.txt", content_type="text/plain",
            source_ref="knowledge:test", chunks_created=1, embeddings_created=1, message="Документ додано.")
        with patch.object(main.knowledge_service, "upload", AsyncMock(return_value=result)), \
             patch.object(main.conversation_service, "finish", AsyncMock(return_value=ChatResult(
                 request_id="test", user_id="api-history", answer="Документ додано.", persistence_outcome="stored"))) as history:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                response = await client.post("/knowledge/upload", data={"user_id": "api-history"},
                    files={"file": ("notes.txt", b"private file contents", "text/plain")})
        self.assertEqual(200, response.status_code)
        call = history.call_args.kwargs
        self.assertEqual("knowledge", call["kind"])
        self.assertEqual("Документ додано.", call["answer"])
        self.assertNotIn("private file contents", str(call))
        self.assertFalse(call["deliver"])

    async def test_news_processing_does_not_enter_conversation(self):
        with patch.object(main.news_processing_service, "process", AsyncMock(return_value=NewsProcessingResult(status="skip"))), \
             patch.object(main.conversation_service, "finish", AsyncMock()) as history:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                response = await client.post("/news/process", json={"title": "News", "user_id": "api-history"})
        self.assertEqual(200, response.status_code)
        history.assert_not_awaited()

    async def test_speech_failure_is_a_user_facing_history_event(self):
        with patch.object(main.speech_to_text_service, "transcribe_url", AsyncMock(side_effect=SpeechToTextError("unavailable"))), \
             patch.object(main.conversation_service, "finish", AsyncMock()) as history:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                response = await client.post("/connector", json={"source": "telegram", "event_type": "attachment",
                    "user_id": "api-history", "reply": {"target": "10"}, "data": {"message_id": 1},
                    "file": {"name": "voice.ogg", "mime_type": "audio/ogg", "url": "http://example.invalid/audio"}})
        self.assertEqual(202, response.status_code)
        call = history.call_args.kwargs
        self.assertEqual("speech", call["kind"])
        self.assertEqual("error", call["execution_outcome"])
        self.assertTrue(call["event_id"].startswith("telegram:"))

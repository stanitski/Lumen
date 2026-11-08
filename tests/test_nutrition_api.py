from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from tests.runtime_test_support import main
from lumen.services.nutrition import NutritionService
from lumen.config import Settings
from lumen.services.chat_only import ChatOnlyService
from tests.test_chat_only_runtime import FakeMemoryService, FakeDeliveryService, FakeOllama


class NutritionApiTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, body):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/connector", json=body)
        tasks = tuple(main.app.state.background_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        return response

    def payload(self, **kwargs):
        return {"source": "telegram", "user_id": "nutrition-api-test", "event_type": "text", "text": "половину",
                "reply": {"target": "10"}, "data": {"message_id": 1, "sent_at": "2026-10-08T10:23:00Z"}, **kwargs}

    async def test_photo_accepted_and_uses_shared_connector_not_stt(self):
        with patch.object(main.chat_service, "process_connector_request", new=AsyncMock()) as route, \
                patch.object(main.speech_to_text_service, "transcribe_url", new=AsyncMock()) as stt:
            response = await self.post(self.payload(event_type="attachment", file={"name": "food.jpg", "mime_type": "image/jpeg", "url": "http://photo"}))
            self.assertEqual(202, response.status_code)
            self.assertEqual("attachment", route.call_args.args[0].event_type)
            stt.assert_not_awaited()

    async def test_text_uses_shared_connector(self):
        with patch.object(main.chat_service, "process_connector_request", new=AsyncMock()) as route:
            self.assertEqual(202, (await self.post(self.payload())).status_code)
            self.assertEqual("половину", route.call_args.args[0].text)
            self.assertEqual("10", route.call_args.kwargs["callback_metadata"]["target_chat_id"])

    async def test_unrelated_text_does_not_invoke_nutrition_model(self):
        memory = FakeMemoryService()
        service = ChatOnlyService(ollama=FakeOllama(), memory_service=memory,
                                  delivery_service=FakeDeliveryService(), settings=Settings(_env_file=None))
        with patch.object(main, "chat_service", service), patch.object(main.nutrition_service, "handle", new=AsyncMock()) as nutrition:
            await self.post(self.payload(text="привіт"))
            nutrition.assert_not_awaited()
            self.assertEqual("привіт", memory.stored_turn["question"])

    async def test_audio_preserves_transcription_and_caption(self):
        with patch.object(main.speech_to_text_service, "transcribe_url", new=AsyncMock(return_value="транскрипт")) as stt, \
                patch.object(main.chat_service, "process_connector_request", new=AsyncMock()) as route:
            await self.post(self.payload(event_type="attachment", text="коментар", file={"name": "voice.ogg", "mime_type": "audio/ogg", "url": "http://voice"}))
            stt.assert_awaited_once()
            event = route.call_args.args[0]
            self.assertEqual("коментар\n\nтранскрипт", event.text)
            self.assertEqual("text", event.event_type)

    async def test_delivery_ack_is_background_and_never_chat(self):
        with patch.object(main.nutrition_service, "handle", new=AsyncMock(return_value=True)) as nutrition, \
                patch.object(main.chat_service, "process_connector_request", new=AsyncMock()) as chat:
            response = await self.post(self.payload(event_type="nutrition_delivery", data={"message_id": 50, "message_tag": "nutrition:meal"}))
            self.assertEqual(202, response.status_code)
            self.assertEqual("nutrition_delivery", response.json()["reason"])
            nutrition.assert_awaited_once()
            chat.assert_not_awaited()

    async def test_feature_disabled_keeps_photo_unsupported(self):
        with patch.object(main.settings, "nutrition_enabled", False):
            response = await self.post(self.payload(event_type="attachment", file={"name": "food.jpg", "mime_type": "image/jpeg"}))
            self.assertEqual("unsupported_attachment", response.json()["reason"])

    async def test_full_http_photo_then_text_correction_updates_single_row(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, nutrition_directory=directory)
            ollama = AsyncMock()
            def result(action, fraction):
                return {"message": {"content": json.dumps({"action": action, "estimate": {
                    "description": "Гречка", "full_portion_kcal": 600, "consumed_fraction": fraction}})}}
            ollama.chat_multimodal.side_effect = [result("record", 1), result("update", .5)]
            delivery = AsyncMock()
            service = NutritionService(settings, ollama, delivery)
            service.download_image = AsyncMock(return_value=(b"\xff\xd8\xffphoto", "image/jpeg"))
            memory = FakeMemoryService()
            memory.query_contract = {**memory._default_query_contract(), "nutrition_action": "update"}
            chat = ChatOnlyService(ollama=FakeOllama(), memory_service=memory,
                                   delivery_service=FakeDeliveryService(), settings=settings)
            with patch.object(main, "nutrition_service", service), patch.object(main, "chat_service", chat):
                await self.post(self.payload(event_type="attachment", text="Гречка", file={"mime_type": "image/jpeg", "url": "http://photo"}))
                await self.post(self.payload(data={"message_id": 2, "sent_at": "2026-10-08T11:00:00Z"}, text="половину"))
                state = service.store.load("nutrition-api-test")
                self.assertEqual(1, len(state.meals))
                self.assertEqual(300, state.meals[0].estimate.calories)
                self.assertEqual("13:23", state.meals[0].eaten_at.strftime("%H:%M"))
                self.assertEqual(2, delivery.deliver_chat_answer.await_count)
                self.assertEqual({}, memory.stored_turn)


if __name__ == "__main__":
    unittest.main()

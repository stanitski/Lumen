from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from lumen.config import Settings
from lumen.models import ConnectorFile, ConnectorReplyTarget, ConnectorRequest
from lumen.services.context_executor import ContextExecutor
from lumen.services.nutrition import NutritionService
from lumen.storage.nutrition import Meal, NutritionEstimate, NutritionState, NutritionStore


class NutritionReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = NutritionStore(self.directory.name)

    def add_meal(self, owner: str, meal_id: str, chat: str, eaten_at: str, kcal: int) -> None:
        state = self.store.load(owner, repair_csv=False)
        moment = datetime.fromisoformat(eaten_at)
        state.meals.append(Meal(id=meal_id, chat_id=chat, recorded_at=moment, eaten_at=moment,
                                estimate=NutritionEstimate(description=meal_id, full_portion_kcal=kcal,
                                                           consumed_fraction=1)))
        self.store.save(state)

    def test_empty_and_populated_ranges_are_available_and_inclusive(self) -> None:
        empty = self.store.read_period("alice", date(2026, 1, 1), date(2026, 1, 2), ZoneInfo("UTC"))
        self.assertEqual(("available", 0, 0), (empty.status, empty.total_calories, empty.meal_count))
        self.add_meal("alice", "breakfast", "chat-a", "2026-01-01T23:30:00+00:00", 300)
        self.add_meal("alice", "dinner", "chat-b", "2026-01-02T10:00:00+00:00", 500)
        self.add_meal("bob", "other-user", "chat-a", "2026-01-01T12:00:00+00:00", 900)

        result = self.store.read_period("alice", date(2026, 1, 2), date(2026, 1, 2), ZoneInfo("Europe/Kyiv"))
        self.assertEqual(("available", 800, 2), (result.status, result.total_calories, result.meal_count))
        self.assertIn("01:30:00+02:00", result.context)
        self.assertNotIn("other-user", result.context)

    def test_corrupt_journal_is_unavailable_and_read_does_not_repair_csv(self) -> None:
        json_path, csv_path = self.store.paths("alice")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("corrupt", encoding="utf-8")
        csv_path.write_text("operator file", encoding="utf-8")
        result = self.store.read_period("alice", date(2026, 1, 1), date(2026, 1, 1))
        self.assertEqual("unavailable", result.status)
        self.assertEqual("operator file", csv_path.read_text(encoding="utf-8"))


class NutritionContextExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_and_read_use_requested_user_and_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = NutritionStore(directory)
            state = NutritionState(user_id="alice")
            moment = datetime.fromisoformat("2026-02-10T11:00:00+00:00")
            state.meals.append(Meal(id="meal", chat_id="chat", recorded_at=moment, eaten_at=moment,
                                    estimate=NutritionEstimate(description="Soup", full_portion_kcal=250,
                                                               consumed_fraction=.5)))
            store.save(state)
            executor = ContextExecutor(nutrition_store=store, nutrition_timezone=ZoneInfo("UTC"))
            results = await executor.execute([
                {"source": "nutrition", "operation": "summary", "date_from": "2026-02-10", "date_to": "2026-02-10"},
                {"source": "nutrition", "operation": "read", "date_from": "2026-02-10", "date_to": "2026-02-10"},
            ], user_id="alice", user_text="show food")
            self.assertEqual(["available", "available"], [item.status for item in results])
            self.assertIn("1 meals, 125 kcal", results[0].context)
            self.assertIn("Soup", results[1].context)
            isolated = await executor.execute([{"source": "nutrition", "operation": "summary",
                                               "date_from": "2026-02-10", "date_to": "2026-02-10"}],
                                              user_id="bob", user_text="show")
            self.assertIn("0 meals, 0 kcal", isolated[0].context)

    async def test_invalid_dates_and_missing_store_are_unavailable(self) -> None:
        executor = ContextExecutor(nutrition_timezone="UTC")
        missing = await executor.execute([{"source": "nutrition", "operation": "read"}],
                                         user_id="alice", user_text="show")
        self.assertEqual("unavailable", missing[0].status)
        with tempfile.TemporaryDirectory() as directory:
            executor = ContextExecutor(nutrition_store=NutritionStore(directory), nutrition_timezone="UTC")
            invalid = await executor.execute([{"source": "nutrition", "operation": "summary",
                                               "date_from": "2026-02-11", "date_to": "2026-02-10"}],
                                              user_id="alice", user_text="show")
            self.assertEqual("unavailable", invalid[0].status)


class FakeConversationService:
    def __init__(self) -> None:
        self.finish = AsyncMock(return_value=type("Result", (), {"persistence_outcome": "ok", "delivery_outcome": "ok"})())


class NutritionSharedHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_retry_uses_shared_history_event_id_and_no_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, nutrition_directory=directory, nutrition_timezone="UTC")
            ollama = AsyncMock()
            ollama.chat_multimodal.return_value = {"message": {"content": json.dumps({
                "action": "record", "estimate": {"description": "Soup", "full_portion_kcal": 200,
                "consumed_fraction": 1}, "meal_date": None, "meal_time": None, "question": "",
                "context": "", "new_entry": False,
            })}}
            conversation = FakeConversationService()
            service = NutritionService(settings, ollama, AsyncMock(), conversation_service=conversation)
            payload = ConnectorRequest(source="telegram", event_type="attachment", user_id="alias",
                                       reply=ConnectorReplyTarget(target="42"), text="Обід",
                                       file=ConnectorFile(name="food.jpg", mime_type="image/jpeg"),
                                       data={"message_id": 7, "sent_at": "2026-02-10T10:00:00Z", "skip_memory": True})
            service.download_image = AsyncMock(return_value=(b"image", "image/jpeg"))
            kwargs = {"user_id": "alice", "request_id": "req", "callback_metadata": {"target_chat_id": "42"}}
            self.assertTrue(await service.handle(payload, **kwargs))
            self.assertTrue(await service.handle(payload, **kwargs))
            self.assertEqual(1, ollama.chat_multimodal.await_count)
            calls = conversation.finish.await_args_list
            self.assertEqual(2, len(calls))
            first = calls[0].kwargs
            self.assertEqual('telegram:["alice","42","7"]', first["event_id"])
            self.assertEqual("Обід", first["question"])
            self.assertEqual("nutrition", first["kind"])
            self.assertEqual("record", first["turn_meta"]["action"])
            self.assertTrue(first["deliver"])
            self.assertTrue(first["callback_metadata"]["nutrition"])
            self.assertEqual(7, first["callback_metadata"]["reply_to_message_id"])
            self.assertTrue(first["callback_metadata"]["message_tag"].startswith("nutrition:"))
            self.assertFalse(first["store_memory"])
            self.assertFalse(calls[1].kwargs["deliver"])

    async def test_clarification_uses_shared_history_with_awaiting_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, nutrition_directory=directory, nutrition_timezone="UTC")
            ollama = AsyncMock()
            ollama.chat_multimodal.return_value = {"message": {"content": json.dumps({
                "action": "clarify", "estimate": None, "meal_date": None, "meal_time": None,
                "question": "Що це за страва?", "context": "", "new_entry": False,
            })}}
            conversation = FakeConversationService()
            service = NutritionService(settings, ollama, AsyncMock(), conversation_service=conversation)
            service.download_image = AsyncMock(return_value=(b"image", "image/jpeg"))
            payload = ConnectorRequest(source="telegram", event_type="attachment", user_id="alice",
                                       reply=ConnectorReplyTarget(target="42"), text="",
                                       file=ConnectorFile(name="food.jpg", mime_type="image/jpeg"),
                                       data={"message_id": 8, "sent_at": "2026-02-10T10:00:00Z"})
            await service.handle(payload, user_id="alice", request_id="req", callback_metadata={})
            call = conversation.finish.await_args.kwargs
            self.assertEqual("[Фото їжі]", call["question"])
            self.assertEqual("awaiting_confirmation", call["execution_outcome"])
            self.assertEqual("clarify", call["turn_meta"]["action"])

    async def test_recent_context_is_bounded_in_analysis_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, nutrition_directory=directory, nutrition_timezone="UTC")
            ollama = AsyncMock()
            ollama.chat_multimodal.return_value = {"message": {"content": json.dumps({"action": "unrelated", "estimate": None})}}
            service = NutritionService(settings, ollama, AsyncMock())
            service.download_image = AsyncMock(return_value=(b"image", "image/jpeg"))
            payload = ConnectorRequest(source="telegram", event_type="attachment", user_id="alice",
                                       reply=ConnectorReplyTarget(target="42"), text="",
                                       file=ConnectorFile(name="food.jpg", mime_type="image/jpeg"),
                                       data={"message_id": 7, "sent_at": "2026-02-10T10:00:00Z"})
            await service.handle(payload, user_id="alice", request_id="req", callback_metadata={},
                                 recent_context=[{"question": "Earlier question", "answer": "Earlier answer"}])
            context = json.loads(ollama.chat_multimodal.call_args.kwargs["messages"][1].content)
            self.assertEqual([{"role": "user", "content": "Earlier question"},
                              {"role": "assistant", "content": "Earlier answer"}], context["recent_context"])


if __name__ == "__main__":
    unittest.main()

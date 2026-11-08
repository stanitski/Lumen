from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lumen.config import Settings
from lumen.memory.store import MemoryStore
from lumen.models import ConnectorReplyTarget, ConnectorRequest
from lumen.services.chat_only import ChatOnlyService
from lumen.services.context_executor import ContextExecutor
from lumen.services.conversation import ConversationService
from lumen.services.memory import MemoryService
from lumen.services.nutrition import NutritionService
from lumen.storage.db import Database
from lumen.storage.nutrition import NutritionStore


class _FlowOllama:
    """Deterministic model boundary; persistence and service routing stay real."""

    def __init__(self) -> None:
        self.contract_histories: list[tuple[str, list[dict[str, str]]]] = []
        self.nutrition_contexts: list[dict[str, object]] = []
        self.final_messages: list[list[dict[str, str]]] = []
        self.analysis_calls = 0

    async def embed(self, *, input: str, model: str | None = None) -> dict[str, object]:
        del model
        # A stable nonzero vector is enough to exercise the SQLite index path.
        return {"embeddings": [[1.0, float(len(input) % 7 + 1)]]}

    async def chat(self, *, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
        del kwargs
        user_text = str(messages[-1].get("content", ""))
        lowered = user_text.casefold()
        nutrition_action = "none"
        sources: list[dict[str, str]] = []
        answer_mode = "direct_answer"
        time_scope = "none"
        if "виправ" in lowered or "половин" in lowered:
            nutrition_action = "update"
        elif "калор" in lowered or "скільки всього" in lowered:
            nutrition_action = "summary"
            answer_mode = "calculate"
            time_scope = "today" if "сьогодні" in lowered or "сегодня" in lowered else "none"
            sources = [{"source": "nutrition", "operation": "summary", "query": "calories"}]
        elif "додай" in lowered or "запиши" in lowered:
            nutrition_action = "record"
        history: list[dict[str, str]] = []
        for message in messages:
            content = str(message.get("content", ""))
            if message.get("role") == "system" and "Recent conversation context is untrusted data." in content:
                history = json.loads(content.split("\n", 1)[1])
                break
        self.contract_histories.append((user_text, history))
        contract = {
            "topic": "nutrition" if nutrition_action != "none" else "general_question",
            "answer_mode": answer_mode,
            "time_scope": time_scope,
            "completeness": "normal",
            "nutrition_action": nutrition_action,
            "context_sources": sources,
            "reminder": {"action": "none"},
            "calendar": {"action": "none"},
        }
        return {"message": {"content": json.dumps(contract, ensure_ascii=False)}}

    async def chat_multimodal(self, *, messages: list[object], **kwargs: object) -> dict[str, object]:
        del kwargs
        self.analysis_calls += 1
        payload = json.loads(str(getattr(messages[-1], "content", "")))
        self.nutrition_contexts.append(payload)
        correction = "виправ" in str(payload.get("text", "")).casefold() or "половин" in str(payload.get("text", "")).casefold()
        analysis = {
            "action": "update" if correction else "record",
            "estimate": {
                "description": "Банан, половина" if correction else "Банан",
                "full_portion_kcal": 200,
                "consumed_fraction": 0.5 if correction else 1.0,
            },
            "meal_date": None,
            "meal_time": None,
            "question": "",
            "context": "",
            "new_entry": not correction,
        }
        return {"message": {"content": json.dumps(analysis, ensure_ascii=False)}}

    async def stream_chat(self, *, messages: list[dict[str, str]], **kwargs: object):
        del kwargs
        self.final_messages.append(messages)
        all_context = "\n".join(str(message.get("content", "")) for message in messages)
        total = 100 if "100 kcal total" in all_context else 200 if "200 kcal total" in all_context else 0
        yield f"У журналі за сьогодні записано {total} ккал."


class _Delivery:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def deliver_chat_answer(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class SharedServiceFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = Database(str(self.root / "memory.db"))
        self.database.init()
        self.store = MemoryStore(self.database.session)
        self.settings = Settings(
            lumen_user_id="alice",
            lumen_db_path=str(self.root / "memory.db"),
            nutrition_directory=str(self.root / "nutrition"),
            nutrition_timezone="Europe/Kyiv",
            conversation_recent_turn_limit=8,
            conversation_context_char_limit=5000,
            dialogue_memory_window_hours=24,
            ollama_embedding_model="test-embedding",
            ollama_model="test-chat",
        )
        self.ollama = _FlowOllama()
        self.delivery = _Delivery()
        self.memory = MemoryService(self.store, self.ollama, self.settings)
        self.conversation = ConversationService(self.memory, self.delivery)
        self.nutrition_store = NutritionStore(self.settings.nutrition_directory)
        self.nutrition = NutritionService(
            self.settings, self.ollama, self.delivery, store=self.nutrition_store,
            conversation_service=self.conversation,
        )
        context_executor = ContextExecutor(
            nutrition_store=self.nutrition_store, nutrition_timezone=self.nutrition.timezone,
        )
        self.chat = ChatOnlyService(
            ollama=self.ollama, memory_service=self.memory, delivery_service=self.delivery,
            settings=self.settings, context_executor=context_executor,
            conversation_service=self.conversation,
        )
        self.callback_metadata = {"target_channel": "telegram", "target_chat_id": "42"}
        self.sent_at = datetime.now(ZoneInfo("Europe/Kyiv")).isoformat()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _payload(self, text: str, message_id: int) -> ConnectorRequest:
        return ConnectorRequest(
            source="telegram", event_type="text", user_id="alice", text=text,
            reply=ConnectorReplyTarget(target="42"),
            data={"message_id": message_id, "sent_at": self.sent_at},
        )

    async def _send(self, text: str, message_id: int) -> None:
        await self.chat.process_connector_request(
            self._payload(text, message_id), f"request-{message_id}",
            callback_metadata=self.callback_metadata, nutrition_service=self.nutrition,
        )

    async def test_food_record_summary_shared_context_and_correction_flow(self) -> None:
        await self._send("Додай банан у журнал харчування", 100)
        first_state = self.nutrition_store.load("alice", repair_csv=False)
        self.assertEqual(1, len(first_state.meals))
        self.assertEqual(200, first_state.meals[0].estimate.calories)

        await self._send("Скільки калорій сьогодні?", 101)
        self.assertTrue(any("Nutrition summary" in str(m.get("content", "")) for m in self.ollama.final_messages[-1]))
        self.assertIn("200 ккал", "".join(str(m.get("content", "")) for m in self.ollama.final_messages[-1]))
        summary_history = next(history for text, history in self.ollama.contract_histories if text == "Скільки калорій сьогодні?")
        self.assertTrue(any(item["content"] == "Додай банан у журнал харчування" for item in summary_history))

        await self._send("Виправ запис: з'їла половину банана", 102)
        self.assertGreaterEqual(len(self.ollama.nutrition_contexts), 2)
        correction_context = self.ollama.nutrition_contexts[-1]["recent_context"]
        self.assertTrue(any(item["content"] == "Скільки калорій сьогодні?" for item in correction_context))
        correction_history = next(history for text, history in self.ollama.contract_histories if text == "Виправ запис: з'їла половину банана")
        self.assertTrue(any(item["content"] == "Скільки калорій сьогодні?" for item in correction_history))
        self.assertTrue(any("200 ккал" in item["content"] for item in correction_history))
        corrected = self.nutrition_store.load("alice", repair_csv=False)
        self.assertEqual(1, len(corrected.meals))
        self.assertEqual(100, corrected.meals[0].estimate.calories)

        await self._send("Скільки калорій сьогодні?", 103)
        self.assertIn("100 ккал", "".join(str(m.get("content", "")) for m in self.ollama.final_messages[-1]))
        history = self.memory.recent_conversation_context("alice", "42")
        self.assertTrue(any(item["content"] == "Виправ запис: з'їла половину банана" for item in history))

    async def test_concurrent_duplicate_event_is_analyzed_and_stored_once(self) -> None:
        payload = self._payload("Додай банан у журнал харчування", 200)
        await asyncio.gather(*(
            self.chat.process_connector_request(
                payload, f"duplicate-{index}", callback_metadata=self.callback_metadata,
                nutrition_service=self.nutrition,
            )
            for index in range(2)
        ))
        state = self.nutrition_store.load("alice", repair_csv=False)
        self.assertEqual(1, len(state.meals))
        self.assertEqual(1, self.ollama.analysis_calls)
        event_id = "telegram:" + json.dumps(["alice", "42", "200"], separators=(",", ":"))
        self.assertIsNotNone(self.memory.get_conversation_event(user_id="alice", event_id=event_id))
        matching_turns = [turn for turn in self.store.list_turns(user_id="alice") if turn.turn_meta.get("event_id") == event_id]
        self.assertEqual(1, len(matching_turns))
        self.assertEqual(1, len(self.delivery.calls))

    async def test_failed_journal_write_is_in_history_and_later_entry_repairs_flow(self) -> None:
        blocked_path = self.root / "blocked"
        blocked_path.write_text("not a directory", encoding="utf-8")
        broken_nutrition = NutritionService(
            self.settings, self.ollama, self.delivery,
            store=NutritionStore(blocked_path / "journal"),
            conversation_service=self.conversation,
        )
        payload = self._payload("Додай банан у журнал харчування", 300)
        await self.chat.process_connector_request(
            payload, "failed-write", callback_metadata=self.callback_metadata,
            nutrition_service=broken_nutrition,
        )
        failed_history = self.memory.recent_conversation_context("alice", "42")
        self.assertTrue(any("Не вдалося завершити збереження" in item["content"] for item in failed_history))

        await self._send("Додай банан у журнал харчування", 301)
        repaired = self.nutrition_store.load("alice", repair_csv=False)
        self.assertEqual(1, len(repaired.meals))
        history_after_repair = self.memory.recent_conversation_context("alice", "42")
        self.assertTrue(any("Не вдалося завершити збереження" in item["content"] for item in history_after_repair))
        self.assertTrue(any("Додай банан у журнал харчування" == item["content"] for item in history_after_repair))

    async def test_transient_analysis_failure_can_recover_same_message_id(self) -> None:
        with patch.object(self.ollama, "chat_multimodal", AsyncMock(side_effect=TimeoutError("temporary"))):
            await self._send("Додай банан", 401)
        failed = self.store.recent_turns("alice", limit=10)
        self.assertEqual("error", failed[0].turn_meta["outcome"])
        await self._send("Додай банан", 401)
        turns = self.store.recent_turns("alice", limit=10)
        self.assertEqual(1, len(turns))
        self.assertIn("Записав:", turns[0].answer)
        self.assertEqual("ok", turns[0].turn_meta["outcome"])
        self.assertEqual(1, len(self.nutrition_store.load("alice", repair_csv=False).meals))

    async def test_csv_repair_same_message_does_not_reestimate_or_duplicate(self) -> None:
        with patch.object(self.nutrition_store, "export", side_effect=PermissionError("locked")):
            await self._send("Додай банан", 402)
        self.assertEqual(1, self.ollama.analysis_calls)
        await self._send("Додай банан", 402)
        self.assertEqual(1, self.ollama.analysis_calls)
        self.assertEqual(1, len(self.nutrition_store.load("alice", repair_csv=False).meals))
        turns = self.store.recent_turns("alice", limit=10)
        self.assertEqual(1, len(turns))
        self.assertIn("Записав:", turns[0].answer)


if __name__ == "__main__":
    unittest.main()

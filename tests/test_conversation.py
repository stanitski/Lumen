from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lumen.config import Settings
from lumen.memory.store import MemoryStore
from lumen.models import ConversationTurnRecord
from lumen.services.conversation import ConversationService
from lumen.services.memory import MemoryService
from lumen.storage.db import Database
from lumen.timezones import kyiv_timezone


class _MemoryStub:
    def __init__(self) -> None:
        self.turns: dict[int, dict[str, object]] = {}
        self.next_id = 1
        self.fail = False
        self.postprocessed: list[int] = []

    def add_turn_once(self, **kwargs: object) -> int:
        if self.fail:
            raise OSError("disk unavailable")
        for turn_id, turn in self.turns.items():
            if turn["event_id"] == kwargs["event_id"]:
                return turn_id
        turn_id = self.next_id
        self.next_id += 1
        self.turns[turn_id] = {**kwargs, "event_id": kwargs["event_id"]}
        return turn_id

    def add_turn(self, **kwargs: object) -> int:
        turn_id = self.next_id
        self.next_id += 1
        self.turns[turn_id] = kwargs
        return turn_id

    def get_conversation_event(self, *, user_id: str, event_id: str):
        for turn_id, turn in self.turns.items():
            if turn.get("user_id") == user_id and turn.get("event_id") == event_id:
                return ConversationTurnRecord(
                    id=turn_id, user_id=user_id, source=str(turn.get("source", "")),
                    question=str(turn.get("question", "")), answer=str(turn.get("answer", "")),
                    question_created_at="", answer_created_at="", created_at="",
                    memory_processed=int(turn.get("memory_processed", 0)),
                    turn_meta={**turn.get("turn_meta", {}), "event_id": event_id},
                )
        return None

    def update_turn_metadata(self, **kwargs: object) -> bool:
        turn = self.turns[int(kwargs["turn_id"])]
        turn_meta = dict(turn.get("turn_meta", {}))
        turn_meta.update(kwargs["updates"])
        turn["turn_meta"] = turn_meta
        return True

    async def postprocess_dialogue_turn(self, *, user_id: str, turn_id: int) -> None:
        self.postprocessed.append(turn_id)
        self.turns[turn_id]["memory_processed"] = 1


class _DeliveryStub:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def deliver_chat_answer(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("callback unavailable")


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_persists_before_delivery_and_updates_status(self) -> None:
        memory = _MemoryStub()
        delivery = _DeliveryStub()
        service = ConversationService(memory, delivery)
        result = await service.finish(
            user_id="alice", question="Hi", answer="Hello", request_id="request-1",
            event_id='["alice","chat","message"]', callback_metadata={"chat_id": "chat"},
        )
        self.assertEqual("stored", result.persistence_outcome)
        self.assertEqual("delivered", result.delivery_outcome)
        self.assertEqual(1, len(delivery.calls))
        self.assertEqual("chat", memory.turns[1]["turn_meta"]["chat_id"])
        self.assertEqual([1], memory.postprocessed)

    async def test_persistence_failure_does_not_deliver(self) -> None:
        memory = _MemoryStub()
        memory.fail = True
        delivery = _DeliveryStub()
        result = await ConversationService(memory, delivery).finish(
            user_id="alice", question="Hi", answer="Hello", request_id="request-1",
            event_id="stable-event",
        )
        self.assertEqual("failed", result.persistence_outcome)
        self.assertEqual("not_attempted", result.delivery_outcome)
        self.assertEqual([], delivery.calls)

    async def test_duplicate_event_returns_persisted_result_without_redelivery(self) -> None:
        memory = _MemoryStub()
        delivery = _DeliveryStub()
        service = ConversationService(memory, delivery)
        kwargs = dict(
            user_id="alice", question="Hi", answer="Hello", request_id="request-1",
            event_id="stable-event",
        )
        await service.finish(**kwargs)
        replay = await service.finish(**{**kwargs, "answer": "different answer", "request_id": "request-2"})
        self.assertEqual("Hello", replay.answer)
        self.assertEqual("request-1", replay.request_id)
        self.assertEqual(1, len(delivery.calls))
        self.assertEqual([1], memory.postprocessed)

    async def test_record_event_persists_without_delivery(self) -> None:
        memory = _MemoryStub()
        delivery = _DeliveryStub()
        turn_id = await ConversationService(memory, delivery).record_event(
            user_id="alice", content="Reminder due", event_id="delivery-1",
        )
        self.assertEqual(1, turn_id)
        self.assertEqual([], delivery.calls)
        self.assertEqual([1], memory.postprocessed)

    async def test_notification_route_is_stored_as_chat_scope(self) -> None:
        memory = _MemoryStub()
        service = ConversationService(memory, _DeliveryStub())
        await service.record_event(user_id="alice", content="Reminder", event_id="notice", metadata={"target_chat_id": "42"})
        event = service.get_event_result(user_id="alice", event_id="notice")
        self.assertEqual("42", event.metadata["chat_id"])

    async def test_background_indexing_does_not_block_persisted_context(self) -> None:
        memory = _MemoryStub()
        delivery = _DeliveryStub()
        gate = asyncio.Event()
        tasks = []
        async def blocked(*, user_id, turn_id):
            await gate.wait()
            memory.postprocessed.append(turn_id)
        memory.postprocess_dialogue_turn = blocked
        def start(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task
        service = ConversationService(memory, delivery, task_starter=start)
        result = await asyncio.wait_for(service.finish(user_id="alice", question="Food", answer="Saved",
            request_id="background", event_id="background-event"), timeout=1)
        self.assertEqual("stored", result.persistence_outcome)
        self.assertEqual("Saved", service.get_event_result(user_id="alice", event_id="background-event").answer)
        self.assertEqual([], memory.postprocessed)
        self.assertEqual(1, len(tasks))
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual([1], memory.postprocessed)


class ConversationMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.directory.name) / "lumen.db"))
        self.database.init()
        self.store = MemoryStore(self.database.session)
        self.settings = Settings(
            lumen_db_path=str(Path(self.directory.name) / "lumen.db"),
            nutrition_directory=str(Path(self.directory.name) / "nutrition"),
            conversation_recent_turn_limit=8,
            conversation_context_char_limit=1000,
        )
        self.memory = MemoryService(self.store, object(), self.settings)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_recent_context_filters_known_other_chat_and_keeps_legacy(self) -> None:
        self.store.add_turn(user_id="alice", source="chat", question="older", answer="legacy")
        self.store.add_turn(
            user_id="alice", source="chat", question="private", answer="other chat",
            turn_meta={"chat_id": "other"},
        )
        self.store.add_turn(
            user_id="alice", source="chat", question="current", answer="current answer",
            turn_meta={"chat_id": "here"},
        )
        context = self.memory.recent_conversation_context("alice", "here")
        self.assertEqual(["older", "legacy", "current", "current answer"], [item["content"] for item in context])

    def test_add_turn_once_is_atomic_idempotency_key_in_metadata(self) -> None:
        first = self.store.add_turn_once(
            user_id="alice", event_id="stable-event", source="telegram", question="Q", answer="A",
        )
        duplicate = self.store.add_turn_once(
            user_id="alice", event_id="stable-event", source="telegram", question="Q2", answer="A2",
        )
        self.assertEqual(first, duplicate)
        self.assertEqual(1, len(self.store.recent_turns("alice", limit=10)))
        self.assertEqual(first, self.store.find_turn_by_event_id(user_id="alice", event_id="stable-event").id)

    def test_recent_context_is_bounded(self) -> None:
        self.settings.conversation_context_char_limit = 10
        context = self.memory._normalize_recent_context([
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "0123456789abcdef"},
        ])
        self.assertEqual([{"role": "assistant", "content": "6789abcdef"}], context)

    def test_chat_filter_is_applied_before_recent_turn_limit(self) -> None:
        self.settings.conversation_recent_turn_limit = 2
        self.store.add_turn(
            user_id="alice", source="telegram", question="current old message", answer="current old answer",
            turn_meta={"chat_id": "here"},
        )
        for index in range(8):
            self.store.add_turn(
                user_id="alice", source="telegram", question=f"other {index}", answer=f"other answer {index}",
                turn_meta={"chat_id": "elsewhere"},
            )
        context = self.memory.recent_conversation_context("alice", "here")
        self.assertEqual(["current old message", "current old answer"], [item["content"] for item in context])

    def test_nutrition_summary_contract_defaults_yesterday_date_and_uses_context_as_data(self) -> None:
        class OllamaStub:
            def __init__(self) -> None:
                self.request = None

            async def chat(self, **kwargs: object) -> dict[str, object]:
                self.request = kwargs
                body = {
                    "topic": "nutrition_summary", "answer_mode": "calculate", "time_scope": "yesterday",
                    "completeness": "normal", "nutrition_action": "summary",
                    "context_sources": [{"source": "nutrition", "operation": "summary"}],
                }
                return {"message": {"content": json.dumps(body)}}

        ollama = OllamaStub()
        service = MemoryService(self.store, ollama, self.settings)
        history = [{"role": "user", "content": "Скільки калорій було за вчора?"}]
        contract = asyncio.run(service.get_contract("А скільки всього?", recent_context=history))
        yesterday = (datetime.now(kyiv_timezone()).date() - timedelta(days=1)).isoformat()
        source = contract["context_sources"][0]
        self.assertEqual("summary", contract["nutrition_action"])
        self.assertEqual(yesterday, source["date_from"])
        self.assertEqual(yesterday, source["date_to"])
        system_context = [
            message["content"] for message in ollama.request["messages"]
            if message["role"] == "system" and "untrusted data" in message["content"]
        ]
        self.assertTrue(system_context)
        self.assertIn("Скільки калорій було за вчора?", system_context[0])

    def test_nutrition_summary_today_uses_configured_timezone(self) -> None:
        class OllamaStub:
            async def chat(self, **kwargs: object) -> dict[str, object]:
                return {"message": {"content": json.dumps({
                    "topic": "nutrition_summary", "answer_mode": "calculate", "time_scope": "today",
                    "completeness": "normal", "nutrition_action": "summary",
                    "context_sources": [{"source": "nutrition", "operation": "summary"}],
                })}}

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = datetime(2026, 1, 1, 11, 30, tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

        self.settings.nutrition_timezone = "Pacific/Kiritimati"
        service = MemoryService(self.store, OllamaStub(), self.settings)
        with patch("lumen.services.memory.datetime", FixedDatetime):
            contract = asyncio.run(service.get_contract("Скільки калорій сьогодні?"))
        source = contract["context_sources"][0]
        self.assertEqual("2026-01-02", source["date_from"])
        self.assertEqual("2026-01-02", source["date_to"])


if __name__ == "__main__":
    unittest.main()

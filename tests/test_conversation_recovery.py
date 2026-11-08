from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lumen.memory.store import MemoryStore
from lumen.models import ConversationTurnRecord
from lumen.services.conversation import ConversationService
from lumen.storage.db import Database


class MemoryStub:
    def __init__(self) -> None:
        self.turns: dict[int, dict[str, object]] = {}
        self.next_id = 1

    def add_turn_once(self, **kwargs: object) -> int:
        for turn_id, turn in self.turns.items():
            if turn["user_id"] == kwargs["user_id"] and turn["event_id"] == kwargs["event_id"]:
                return turn_id
        turn_id = self.next_id
        self.next_id += 1
        self.turns[turn_id] = {**kwargs, "user_id": kwargs["user_id"], "memory_processed": 0}
        return turn_id

    def add_turn(self, **kwargs: object) -> int:
        turn_id = self.next_id
        self.next_id += 1
        self.turns[turn_id] = {**kwargs, "user_id": kwargs["user_id"]}
        return turn_id

    def get_conversation_event(self, *, user_id: str, event_id: str) -> ConversationTurnRecord | None:
        for turn_id, turn in self.turns.items():
            if turn.get("user_id") == user_id and turn.get("event_id") == event_id:
                return ConversationTurnRecord(
                    id=turn_id, user_id=user_id, source=str(turn.get("source", "")),
                    question=str(turn.get("question", "")), answer=str(turn.get("answer", "")),
                    question_created_at="", answer_created_at="", created_at="",
                    memory_processed=int(turn.get("memory_processed", 0)),
                    turn_meta=dict(turn.get("turn_meta", {})),
                )
        return None

    def update_turn_result(self, *, user_id: str, turn_id: int, question: str, answer: str,
                           updates: dict[str, object]) -> bool:
        turn = self.turns.get(turn_id)
        if turn is None or turn.get("user_id") != user_id:
            return False
        turn.update(question=question, answer=answer, turn_meta=dict(updates), memory_processed=0)
        return True

    def update_turn_metadata(self, *, user_id: str, turn_id: int, updates: dict[str, object]) -> bool:
        turn = self.turns.get(turn_id)
        if turn is None or turn.get("user_id") != user_id:
            return False
        metadata = dict(turn.get("turn_meta", {}))
        metadata.update(updates)
        turn["turn_meta"] = metadata
        return True

    async def postprocess_dialogue_turn(self, *, user_id: str, turn_id: int) -> None:
        self.turns[turn_id]["memory_processed"] = 1


class DeliveryStub:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls: list[dict[str, object]] = []

    async def deliver_chat_answer(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("callback unavailable")


class ConversationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_retry_replaces_delivered_error_in_one_event_row(self) -> None:
        memory = MemoryStub()
        delivery = DeliveryStub()
        service = ConversationService(memory, delivery)
        common = {"user_id": "alice", "question": "[Фото їжі]", "request_id": "first",
                  "source": "telegram", "kind": "nutrition", "event_id": 'telegram:["alice","42","7"]'}
        first = await service.finish(
            **common, answer="Не вдалося отримати оцінку.", execution_outcome="error",
            callback_metadata={"target_chat_id": "42", "nutrition": True},
            turn_meta={"entry_id": None, "ref_id": None, "action": "error"},
        )
        self.assertTrue(first.delivered)
        self.assertEqual(1, len(memory.turns))

        second = await service.finish(
            **{**common, "request_id": "retry"}, answer="Записав: суп, 250 ккал.",
            callback_metadata={"target_chat_id": "42", "nutrition": True, "message_tag": "nutrition:new-entry",
                               "reply_to_message_id": 7},
            turn_meta={"entry_id": "meal-1", "ref_id": "meal-1", "action": "record"},
        )

        self.assertEqual("Записав: суп, 250 ккал.", second.answer)
        self.assertEqual(1, len(memory.turns))
        row = memory.turns[1]
        self.assertEqual("Записав: суп, 250 ккал.", row["answer"])
        metadata = row["turn_meta"]
        self.assertEqual("ok", metadata["outcome"])
        self.assertEqual("record", metadata["action"])
        self.assertEqual("nutrition:new-entry", metadata["callback_metadata"]["message_tag"])
        self.assertEqual("delivered", metadata["delivery_status"])
        self.assertEqual("nutrition:new-entry", delivery.calls[1]["metadata"]["message_tag"])

        duplicate = await service.finish(
            **{**common, "request_id": "third"}, answer="different", execution_outcome="ok",
        )
        self.assertEqual("Записав: суп, 250 ккал.", duplicate.answer)
        self.assertEqual(2, len(delivery.calls))

    async def test_failed_delivery_retry_uses_saved_nutrition_callback_metadata(self) -> None:
        memory = MemoryStub()
        delivery = DeliveryStub(fail_first=True)
        service = ConversationService(memory, delivery)
        common = {"user_id": "alice", "question": "половину", "answer": "Виправив: суп, 125 ккал.",
                  "source": "telegram", "kind": "nutrition", "event_id": 'telegram:["alice","42","8"]'}
        original = {"target_channel": "telegram", "target_chat_id": "42", "nutrition": True,
                    "message_tag": "nutrition:meal-1", "reply_to_message_id": 8}
        first = await service.finish(**common, request_id="first", callback_metadata=original,
                                     turn_meta={"action": "update", "ref_id": "meal-1"})
        self.assertEqual("failed", first.delivery_outcome)

        retried = await service.finish(
            **{**common, "request_id": "retry"},
            callback_metadata={"target_channel": "telegram", "target_chat_id": "42"},
            turn_meta={"action": "update"},
        )

        self.assertEqual("delivered", retried.delivery_outcome)
        self.assertEqual(1, len(memory.turns))
        replay_metadata = delivery.calls[1]["metadata"]
        self.assertTrue(replay_metadata["nutrition"])
        self.assertEqual("nutrition:meal-1", replay_metadata["message_tag"])
        self.assertEqual(8, replay_metadata["reply_to_message_id"])


class MemoryStoreResultUpdateTests(unittest.TestCase):
    def test_update_turn_result_atomically_replaces_content_and_resets_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "memory.db"))
            database.init()
            store = MemoryStore(database.session)
            turn_id = store.add_turn_once(
                user_id="alice", event_id="event-1", source="telegram", question="photo", answer="failed",
                turn_meta={"outcome": "error", "callback_accepted": True},
            )
            with database.session() as connection:
                connection.execute(
                    "UPDATE conversation_turns SET memory_processed = 1, question_embedding_json = '[1]', answer_embedding_json = '[1]' WHERE id = ?",
                    (turn_id,),
                )
                connection.commit()

            updated = store.update_turn_result(
                user_id="alice", turn_id=turn_id, question="photo", answer="recorded",
                updates={"outcome": "ok", "action": "record", "callback_accepted": False},
            )

            self.assertTrue(updated)
            turn = store.find_turn_by_event_id(user_id="alice", event_id="event-1")
            self.assertIsNotNone(turn)
            self.assertEqual("recorded", turn.answer)
            self.assertEqual("ok", turn.turn_meta["outcome"])
            self.assertEqual("event-1", turn.turn_meta["event_id"])
            self.assertEqual(0, turn.memory_processed)
            with database.session() as connection:
                row = connection.execute(
                    "SELECT primary_topic, primary_topic_embedding_json, question_embedding_json, answer_embedding_json FROM conversation_turns WHERE id = ?",
                    (turn_id,),
                ).fetchone()
            self.assertEqual(("", "[]", "[]", "[]"), tuple(row))
            self.assertFalse(store.update_turn_result(
                user_id="bob", turn_id=turn_id, question="x", answer="y", updates={},
            ))


if __name__ == "__main__":
    unittest.main()

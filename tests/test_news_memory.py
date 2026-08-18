from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from lumen.config import Settings
from lumen.memory.store import MemoryStore
from lumen.services.memory import MemoryService
from lumen.storage.db import Database
from lumen.time_utils import LOCAL_TIMEZONE


class FakeOllama:
    def __init__(self, content: str = '{"facts": []}') -> None:
        self.content = content
        self.messages: list[dict[str, str]] = []

    async def chat(self, *, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
        del kwargs
        self.messages = messages
        return {"message": {"content": self.content}}


class DeterministicMemoryService(MemoryService):
    async def get_embedding(self, text: str) -> list[float]:
        del text
        return [1.0]


class NewsMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.db_path = str(Path(self._temporary_directory.name) / "lumen-news-memory.db")
        self._previous_db_path = os.environ.get("LUMEN_DB_PATH")
        os.environ["LUMEN_DB_PATH"] = self.db_path
        self.addCleanup(self._restore_db_path)
        self.database = Database(self.db_path)
        self.database.init()
        self.store = MemoryStore(self.database.session)
        self.ollama = FakeOllama()
        self.service = DeterministicMemoryService(self.store, self.ollama, Settings())

    def _restore_db_path(self) -> None:
        if self._previous_db_path is None:
            os.environ.pop("LUMEN_DB_PATH", None)
        else:
            os.environ["LUMEN_DB_PATH"] = self._previous_db_path

    def _insert_fact(self, *, user_id: str, subject: str, predicate: str, expires_at: str | None = None) -> None:
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO memory_facts(
                    user_id, category, subject, predicate, value, confidence, importance,
                    source_ref, last_seen, expires_at, tags_json
                )
                VALUES (?, 'news_context', ?, ?, 'legacy', 0.5, 4, 'legacy',
                        '2026-08-01T12:00:00+03:00', ?, '[]')
                """,
                (user_id, subject, predicate, expires_at),
            )
            connection.commit()

    def test_upsert_collapses_duplicates_and_legacy_likes(self) -> None:
        subject = "topic:гороскопи"
        self._insert_fact(user_id="VST", subject=subject, predicate="likes")
        self._insert_fact(user_id="VST", subject=subject, predicate="dislikes")
        self._insert_fact(user_id="VST", subject=subject, predicate="dislikes")

        stored = self.service.store_memory_facts(
            [self.service._news_dislike_fact(user_id="VST", topic="Гороскопи", value="Гороскопи")],
            source_ref="dialogue:turn:1",
            user_id="VST",
        )

        self.assertEqual(1, len(stored))
        self.assertEqual("news_context", stored[0].category)
        active = self.service.list_active_news_dislikes("VST")
        self.assertEqual(1, len(active))
        self.assertEqual(subject, active[0]["subject"])
        self.assertEqual("dislikes", active[0]["predicate"])
        self.assertEqual(["news_filter"], active[0]["tags"])
        self.assertIsNone(active[0]["expires_at"])
        with self.database.session() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_facts WHERE user_id = ? AND category = 'news_context' AND subject = ?",
                ("VST", subject),
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_active_read_is_user_scoped_and_excludes_expired_facts(self) -> None:
        self.store.upsert_news_dislike(
            user_id="VST", subject="topic:спорт", value="спорт", confidence=1.0,
            importance=8, source_ref="test",
        )
        self._insert_fact(user_id="global", subject="topic:світ", predicate="dislikes")
        self._insert_fact(user_id="other-user", subject="topic:політика", predicate="dislikes")
        self._insert_fact(
            user_id="VST", subject="topic:старе", predicate="dislikes",
            expires_at=(datetime.now(LOCAL_TIMEZONE) - timedelta(days=1)).isoformat(),
        )

        self.assertEqual(["topic:спорт"], [fact["subject"] for fact in self.service.list_active_news_dislikes("VST")])

    async def test_commands_create_dislikes_and_remove_existing_topic(self) -> None:
        facts = await self.service.extract_memory_facts("Не показуй   Гороскопи!", "VST")

        self.assertEqual(1, len(facts))
        self.assertEqual("news_context", facts[0]["category"])
        self.assertEqual("topic:гороскопи", facts[0]["subject"])
        self.assertEqual("dislikes", facts[0]["predicate"])
        self.assertEqual(["news_filter"], facts[0]["tags"])
        self.assertEqual("permanent", facts[0]["retention_policy"])
        self.assertIn("Never create likes for news", self.ollama.messages[0]["content"])
        self.service.store_memory_facts(facts, source_ref="dialogue:turn:2", user_id="VST")

        turn_id = self.store.add_turn(
            user_id="VST", source="test", question="Прибери заборону на гороскопи", answer="Гаразд",
        )
        await self.service.postprocess_dialogue_turn(user_id="VST", turn_id=turn_id)

        self.assertEqual([], self.service.list_active_news_dislikes("VST"))
        removal_facts = await self.service.extract_memory_facts("Знову показуй гороскопи", "VST")
        self.assertFalse(any(fact["category"] == "news_context" for fact in removal_facts))

    def test_remove_news_dislike_deletes_legacy_duplicates(self) -> None:
        subject = "topic:езотерика"
        self._insert_fact(user_id="VST", subject=subject, predicate="likes")
        self._insert_fact(user_id="VST", subject=subject, predicate="dislikes")

        self.assertEqual(2, self.service.remove_news_dislike(user_id="VST", topic="Езотерика"))
        self.assertEqual([], self.service.list_active_news_dislikes("VST"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from lumen.memory.fact_policy import FACT_EMBEDDING_SOURCE_TYPE, fact_content_hash, fact_embedding_text
from lumen.memory.store import MemoryStore
from lumen.services.fact_context import FactContextService
from lumen.storage.db import Database


class FakeOllama:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.on_embed = None

    async def embed(self, input: str, model: str | None = None, **kwargs: object) -> dict[str, object]:
        del model, kwargs
        self.calls.append(input)
        if self.fail:
            raise RuntimeError("embedding unavailable")
        if self.on_embed is not None:
            callback, self.on_embed = self.on_embed, None
            callback()
        # Explicit fixtures use these vectors as query and index values.
        lowered = input.casefold()
        return {"embeddings": [[1.0, 0.0] if "коти" in lowered else [0.0, 1.0]]}


class FactContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(str(Path(self.tmp.name) / "facts.db"))
        self.database.init()
        self.store = MemoryStore(self.database.session)
        self.ollama = FakeOllama()
        self.settings = SimpleNamespace(
            fact_semantic_min_similarity=0.55,
            fact_context_item_limit=10,
            fact_context_char_limit=2400,
            ollama_embedding_model="fake-model",
        )
        self.service = FactContextService(self.store, self.ollama, self.settings)

    def add_fact(
        self,
        *,
        user_id: str = "VST",
        category: str = "preference",
        subject: str = "style",
        predicate: str = "prefers",
        value: str = "коти",
        importance: int = 5,
        tags: list[str] | None = None,
        expires_at: str | None = None,
    ) -> int:
        with self.database.session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_facts(
                    user_id, category, subject, predicate, value, confidence, importance,
                    source_ref, last_seen, expires_at, tags_json
                ) VALUES (?, ?, ?, ?, ?, 1.0, ?, 'test', ?, ?, ?)
                """,
                (
                    user_id,
                    category,
                    subject,
                    predicate,
                    value,
                    importance,
                    datetime.now(timezone.utc).isoformat(),
                    expires_at,
                    "[]" if tags is None else __import__("json").dumps(tags),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    async def test_mandatory_all_and_scope_security_expiry(self) -> None:
        mandatory_ids = [
            self.add_fact(subject=f"rule-{index}", value=f"правило {index}", tags=["lumen:mandatory"])
            for index in range(60)
        ]
        self.add_fact(user_id="other", subject="other", value="не успадковувати")
        self.add_fact(category="secret", subject="token", value="hidden")
        self.add_fact(
            subject="expired",
            value="old",
            expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        context = await self.service.build(user_id="VST", payload_text="інструкція")
        self.assertEqual(mandatory_ids, [fact["id"] for fact in context["mandatory_facts"]])
        self.assertEqual(60, len(context["mandatory_facts"]))
        self.assertNotIn("hidden", context["mandatory_context"])
        self.assertNotIn("other", context["mandatory_context"])
        self.assertIn("Permanent user communication rules:", context["mandatory_context"])

    async def test_semantic_threshold_lexical_fallback_and_budgets(self) -> None:
        cat_id = self.add_fact(subject="домашні тварини", value="коти", importance=9)
        self.add_fact(subject="German", value="unabhängig", importance=10)
        self.settings.fact_context_item_limit = 1
        self.settings.fact_context_char_limit = 90
        await self.service.sync_index(user_id="VST")
        context = await self.service.build(user_id="VST", payload_text="коти", query_vector=[1.0, 0.0])
        self.assertEqual([cat_id], [fact["id"] for fact in context["contextual_facts"]])
        self.assertEqual(context["selected_facts"], [*context["mandatory_facts"], *context["contextual_facts"]])
        self.assertEqual(1, context["fact_retrieval"]["selected_contextual"])
        self.assertLessEqual(len(context["memory_context"]), self.settings.fact_context_char_limit)

    async def test_global_mandatory_tag_is_contextual_and_other_users_are_excluded(self) -> None:
        self.add_fact(user_id="global", subject="shared", value="shared rule", tags=["lumen:mandatory"])
        self.add_fact(user_id="other", subject="private", value="private")
        context = await self.service.build(user_id="VST", payload_text="shared", query_vector=[])
        self.assertEqual([], context["mandatory_facts"])
        self.assertEqual(["global"], [fact["user_id"] for fact in context["contextual_facts"]])
        self.assertIn("Relevant global memory facts:", context["memory_context"])
        self.assertNotIn("private", context["memory_context"])

    async def test_sync_missing_stale_model_delete_and_idempotent(self) -> None:
        fact_id = self.add_fact(subject="коти", value="коти")
        first = await self.service.sync_index(user_id="VST")
        self.assertEqual(1, first["indexed"])
        second = await self.service.sync_index(user_id="VST")
        self.assertEqual(1, second["current"])
        self.settings.ollama_embedding_model = "new-model"
        changed = await self.service.sync_index(user_id="VST")
        self.assertEqual(1, changed["indexed"])
        # Simulate an old row removed by an external writer while its index
        # remains, which the index sweep must clean up.
        with self.database.session() as connection:
            connection.execute("DELETE FROM memory_facts WHERE user_id = ? AND id = ?", ("VST", fact_id))
            connection.commit()
        removed = await self.service.sync_index(user_id="VST")
        self.assertEqual(1, removed["removed"])
        self.assertEqual([], self.store.list_embeddings(user_id="VST", source_types=[FACT_EMBEDDING_SOURCE_TYPE]))

    async def test_embedding_failure_falls_back_without_cancellation_swallow(self) -> None:
        self.add_fact(subject="коти", value="коти")
        self.ollama.fail = True
        context = await self.service.build(user_id="VST", payload_text="коти")
        self.assertEqual("semantic_unavailable", context["fact_retrieval"]["status"])
        self.assertEqual(1, len(context["contextual_facts"]))
        self.ollama.fail = False
        task = asyncio.create_task(self.service.build(user_id="VST", payload_text="коти"))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_read_rejects_external_stale_update_before_sync(self) -> None:
        fact_id = self.add_fact(subject="коти", value="старе значення")
        await self.service.sync_index(user_id="VST")
        with self.database.session() as connection:
            connection.execute("UPDATE memory_facts SET value = ? WHERE user_id = ? AND id = ?", ("нове", "VST", fact_id))
            connection.commit()
        context = await self.service.build(user_id="VST", payload_text="старе", query_vector=[1.0, 0.0])
        self.assertEqual([], context["contextual_facts"])
        self.assertGreaterEqual(context["fact_retrieval"]["rejected_stale"], 1)

    async def test_read_rejects_model_and_dimension_mismatch(self) -> None:
        fact_id = self.add_fact(subject="коти", value="коти")
        await self.service.sync_index(user_id="VST")
        with self.database.session() as connection:
            connection.execute(
                "UPDATE memory_embeddings SET metadata_json = ? WHERE user_id = ? AND source_ref = ?",
                (json.dumps({"content_hash": fact_content_hash(self.store.get_fact("VST", fact_id)), "embedding_model": "wrong"}), "VST", f"memory:fact:{fact_id}"),
            )
            connection.commit()
        mismatched_model = await self.service.build(user_id="VST", payload_text="semantic-only", query_vector=[1.0, 0.0])
        self.assertEqual([], mismatched_model["contextual_facts"])
        self.assertGreaterEqual(mismatched_model["fact_retrieval"]["rejected_stale"], 1)
        with self.database.session() as connection:
            connection.execute(
                "UPDATE memory_embeddings SET metadata_json = ?, embedding_json = ? WHERE user_id = ? AND source_ref = ?",
                (json.dumps({"content_hash": fact_content_hash(self.store.get_fact("VST", fact_id)), "embedding_model": "fake-model"}), "[1.0, 0.0, 0.0]", "VST", f"memory:fact:{fact_id}"),
            )
            connection.commit()
        mismatched_dimension = await self.service.build(user_id="VST", payload_text="semantic-only", query_vector=[1.0, 0.0])
        self.assertEqual([], mismatched_dimension["contextual_facts"])
        self.assertEqual(0, mismatched_dimension["fact_retrieval"]["semantic_hits"])

    async def test_followup_embedding_uses_combined_current_and_previous_text(self) -> None:
        await self.service.build(
            user_id="VST",
            payload_text="новий запит",
            query_vector=[1.0, 0.0],
            previous_question="попереднє питання",
        )
        self.assertEqual(["попереднє питання\nновий запит"], self.ollama.calls)
        self.assertNotIn("попереднє питання", self.ollama.calls)

    async def test_sync_rechecks_fact_after_embedding_race(self) -> None:
        fact_id = self.add_fact(subject="коти", value="до зміни")

        def mutate_fact() -> None:
            with self.database.session() as connection:
                connection.execute("UPDATE memory_facts SET value = ? WHERE user_id = ? AND id = ?", ("після зміни", "VST", fact_id))
                connection.commit()

        self.ollama.on_embed = mutate_fact
        result = await self.service.sync_index(user_id="VST")
        self.assertEqual(0, result["indexed"])
        self.assertEqual(1, result["stale_race"])
        self.assertEqual([], self.store.list_embeddings(user_id="VST", source_types=[FACT_EMBEDDING_SOURCE_TYPE]))

    async def test_sync_index_is_idempotent_beyond_one_thousand_indexes(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        facts: list[dict[str, object]] = []
        with self.database.session() as connection:
            for index in range(1001):
                subject = f"bulk-{index}"
                value = f"value-{index}"
                cursor = connection.execute(
                    """
                    INSERT INTO memory_facts(
                        user_id, category, subject, predicate, value, confidence, importance,
                        source_ref, last_seen, expires_at, tags_json
                    ) VALUES ('VST', 'preference', ?, 'prefers', ?, 1.0, 5, 'bulk', ?, NULL, '[]')
                    """,
                    (subject, value, now),
                )
                fact = {"id": int(cursor.lastrowid), "user_id": "VST", "category": "preference", "subject": subject, "predicate": "prefers", "value": value, "confidence": 1.0, "importance": 5, "source_ref": "bulk", "last_seen": now, "expires_at": None, "tags": []}
                facts.append(fact)
                connection.execute(
                    """
                    INSERT INTO memory_embeddings(
                        user_id, source_type, source_ref, chunk_index, content, embedding_json,
                        importance, created_at, metadata_json, expires_at
                    ) VALUES ('VST', ?, ?, 0, ?, '[1.0, 0.0]', 5, ?, ?, NULL)
                    """,
                    (FACT_EMBEDDING_SOURCE_TYPE, f"memory:fact:{fact['id']}", fact_embedding_text(fact), now, json.dumps({"content_hash": fact_content_hash(fact), "embedding_model": "fake-model"})),
                )
            connection.commit()
        first = await self.service.sync_index(user_id="VST")
        second = await self.service.sync_index(user_id="VST")
        self.assertEqual(1001, first["current"])
        self.assertEqual(1001, second["current"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from lumen.config import Settings
from lumen.memory.fact_policy import MANDATORY_TAG, fact_content_hash
from lumen.memory.store import MemoryStore
from lumen.services.chat_only import ChatOnlyService
from lumen.services.memory import MemoryService
from lumen.storage.db import Database
from scripts.prepare_fact_memory import apply_manifest, prepare


class LocalOllama:
    def __init__(self):
        self.facts = []
        self.messages = []

    async def embed(self, *, input, model):
        return {"embeddings": [[1.0, 0.0]]}

    async def chat(self, *, messages, **kwargs):
        self.messages = messages
        return {"message": {"content": json.dumps({"facts": self.facts})}}


class FactMemoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.db"
        self.db = Database(str(self.path))
        self.db.init()
        self.store = MemoryStore(self.db.session)
        self.ollama = LocalOllama()
        self.settings = Settings(lumen_db_path=str(self.path))
        self.memory = MemoryService(self.store, self.ollama, self.settings)
        self.memory.get_contract = AsyncMock(return_value=self.memory._default_query_contract())

    def add(self, subject="communication_language", value="українська", *, user="alice", mandatory=True):
        return self.store.add_fact(user_id=user, category="preference", subject=subject,
                                   predicate="prefers", value=value, confidence=1.0, importance=6,
                                   source_ref="test", tags=[MANDATORY_TAG] if mandatory else [])

    async def test_greeting_final_prompt_contains_all_mandatory_rules_and_no_unrelated_preferences(self):
        ids = [self.add(f"rule-{i}", f"Standing preference {i}") for i in range(6)]
        unrelated = self.add("coffee", "with milk", mandatory=False)
        self.add("other-user", "PRIVATE RULE", user="bob")
        chat = ChatOnlyService(ollama=self.ollama, memory_service=self.memory,
                               delivery_service=Mock(), settings=self.settings)
        messages, context = await chat._build_dialogue_messages(user_text="Привіт", user_id="alice")
        self.assertEqual(set(ids), {f["id"] for f in context["mandatory_facts"]})
        self.assertNotIn(unrelated, {f["id"] for f in context["selected_facts"]})
        prompt = "\n".join(m["content"] for m in messages)
        for i in range(6):
            self.assertIn(f"Standing preference {i}", prompt)
        self.assertNotIn("PRIVATE RULE", prompt)
        self.memory.get_contract.assert_awaited_once()

    async def test_each_answer_mode_allows_contextual_fact_search(self):
        fact_id = self.add("coffee", "milk", mandatory=False)
        for mode, text in (("direct_answer", "coffee"), ("calculate", "Calculate coffee cost"),
                           ("list_records", "List coffee preferences"), ("continue_context", "And coffee?")):
            with self.subTest(mode=mode):
                contract = {**self.memory._default_query_contract(), "answer_mode": mode}
                context = await self.memory.build_planner_context(
                    payload_text=text, user_id="alice", query_contract=contract, force_semantic_dialogue=True)
                self.assertTrue(context["retrieval_policy"]["include_facts"])
                self.assertIn(fact_id, {f["id"] for f in context["contextual_facts"]})

    async def test_embedding_outage_does_not_prevent_final_mandatory_context(self):
        fact_id = self.add()
        self.ollama.embed = AsyncMock(side_effect=RuntimeError("offline"))
        chat = ChatOnlyService(ollama=self.ollama, memory_service=self.memory,
                               delivery_service=Mock(), settings=self.settings)
        messages, context = await chat._build_dialogue_messages(user_text="Поясни механізм пам’яті", user_id="alice")
        self.assertIn(fact_id, {f["id"] for f in context["mandatory_facts"]})
        self.assertIn("українська", "\n".join(m["content"] for m in messages))

    async def test_followup_passes_previous_question_but_independent_request_does_not(self):
        self.memory.recent_dialogue_turns = Mock(return_value=[SimpleNamespace(question="coffee budget")])
        real_build = self.memory._fact_context.build
        self.memory._fact_context.build = AsyncMock(side_effect=real_build)
        await self.memory.build_planner_context(payload_text="А скільки?", user_id="alice",
            query_contract={**self.memory._default_query_contract(), "answer_mode": "continue_context"})
        self.assertEqual("coffee budget", self.memory._fact_context.build.call_args.kwargs["previous_question"])
        await self.memory.build_planner_context(payload_text="Explain the moon", user_id="alice")
        self.assertEqual("", self.memory._fact_context.build.call_args.kwargs["previous_question"])

    async def test_extraction_explicit_policy_update_and_revocation_preserve_identity(self):
        fact_id = self.add()
        self.ollama.facts = [{"fact_id": fact_id, "category": "preference", "subject": "communication_language",
                              "predicate": "prefers", "value": "українська", "inclusion": "contextual", "tags": ["language"]}]
        facts = await self.memory.extract_memory_facts("Не застосовуй це правило завжди", "alice")
        self.memory.store_memory_facts(facts, source_ref="test:update", user_id="alice")
        self.assertNotIn(MANDATORY_TAG, self.store.get_fact("alice", fact_id)["tags"])
        self.assertEqual(1, len(self.store.list_facts("alice", include_global=False)))
        prompt = "\n".join(m["content"] for m in self.ollama.messages)
        self.assertIn("one-off override must not change stored policy", prompt)
        self.assertIn("task-local instruction", prompt)

    def test_untrusted_marker_and_foreign_id_cannot_promote_or_modify_fact(self):
        foreign = self.add(user="bob")
        raw = {"category": "preference", "subject": "coffee", "predicate": "prefers", "value": "milk",
               "tags": [MANDATORY_TAG]}
        normalized = self.memory.normalize_memory_facts([raw], "alice")
        self.assertNotIn(MANDATORY_TAG, normalized[0]["tags"])
        self.assertEqual([], self.memory.normalize_memory_facts([{**raw, "fact_id": foreign}], "alice"))
        secret = self.memory.normalize_memory_facts([{**raw, "category": "secret", "inclusion": "mandatory"}], "alice")
        self.assertNotIn(MANDATORY_TAG, secret[0]["tags"])

    def test_mandatory_marker_survives_tag_budget_and_has_permanent_retention(self):
        normalized = self.memory.normalize_memory_facts([{"category": "preference", "subject": "style",
            "predicate": "prefers", "value": "short", "inclusion": "mandatory", "tags": [f"tag{i}" for i in range(12)],
            "expires_at": "2030-01-01T00:00:00+00:00", "retention_policy": "ephemeral_24h"}], "alice")[0]
        self.assertIn(MANDATORY_TAG, normalized["tags"])
        self.assertIsNone(normalized["expires_at"])
        self.assertEqual("permanent", normalized["retention_policy"])

    def test_explicit_profile_update_checks_canonical_identity(self):
        fact_id = self.store.add_fact(user_id="alice", category="profile", subject="current_user",
            predicate="lives_in", value="Kyiv", confidence=1.0, importance=6, source_ref="test")
        normalized = self.memory.normalize_memory_facts([{"fact_id": fact_id, "category": "profile",
            "predicate": "LIVES IN", "value": "Lviv", "inclusion": "contextual"}], "alice")
        self.assertEqual("current_user", normalized[0]["subject"])
        self.memory.store_memory_facts(normalized, source_ref="test:update", user_id="alice")
        self.assertEqual("Lviv", self.store.get_fact("alice", fact_id)["value"])

    def test_distinct_preferences_cannot_silently_overwrite_same_identity(self):
        common = {"category": "preference", "subject": "current_user", "predicate": "prefers", "inclusion": "mandatory"}
        facts = self.memory.normalize_memory_facts([
            {**common, "value": "Ukrainian"}, {**common, "value": "informal address"},
        ], "alice")
        self.assertEqual([], facts)

    async def test_prepare_preview_is_read_only_and_apply_creates_backup(self):
        fact_id = self.add(mandatory=False)
        before = self.store.get_fact("alice", fact_id)
        manifest = Path(self.tmp.name) / "manifest.json"
        manifest.write_text(json.dumps([{"id": fact_id, "expected_hash": fact_content_hash(before), "inclusion": "mandatory"}]))
        preview = await prepare(db_path=self.path, user_id="alice", manifest_path=manifest)
        self.assertEqual("preview", preview["mode"])
        self.assertEqual(before, self.store.get_fact("alice", fact_id))
        result = await prepare(db_path=self.path, user_id="alice", manifest_path=manifest, apply=True)
        self.assertTrue(Path(result["backup"]).is_file())
        after = self.store.get_fact("alice", fact_id)
        self.assertIn(MANDATORY_TAG, after["tags"])
        self.assertEqual({k: v for k, v in before.items() if k != "tags"}, {k: v for k, v in after.items() if k != "tags"})

    def test_manifest_conflict_rolls_back_all_tags(self):
        a, b = self.add("one", mandatory=False), self.add("two", mandatory=False)
        manifest = [{"id": a, "expected_hash": fact_content_hash(self.store.get_fact("alice", a)), "inclusion": "mandatory"},
                    {"id": b, "expected_hash": "0" * 64, "inclusion": "mandatory"}]
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            with self.assertRaises(ValueError):
                apply_manifest(conn, "alice", manifest)
        self.assertNotIn(MANDATORY_TAG, self.store.get_fact("alice", a)["tags"])


if __name__ == "__main__":
    unittest.main()

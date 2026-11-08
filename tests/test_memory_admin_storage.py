from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lumen.memory.fact_policy import FACT_EMBEDDING_SOURCE_PREFIX, FACT_EMBEDDING_SOURCE_TYPE, fact_revision
from lumen.memory.store import MemoryStore
from lumen.storage.db import Database


class MemoryAdminStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.directory.name) / "memory.db"))
        self.database.init()
        self.store = MemoryStore(self.database.session)
        self.fact_id = self._add("alice")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _add(self, user_id: str) -> int:
        return self.store.add_fact(
            user_id=user_id, category="profile", subject="contact", predicate="is",
            value="old value", confidence=0.9, importance=5, source_ref="test",
            tags=["contact"],
        )

    def _edit(self, value: str, revision: str | None = None, user_id: str = "alice") -> dict | None:
        return self.store.update_fact(
            self.fact_id, user_id=user_id, category="profile", subject="contact",
            predicate="is", value=value, confidence=0.9, importance=5,
            source_ref="test", tags=["contact"], expected_revision=revision,
        )

    def _index(self) -> None:
        self.store.add_embedding(
            user_id="alice", source_type=FACT_EMBEDDING_SOURCE_TYPE,
            source_ref=f"{FACT_EMBEDDING_SOURCE_PREFIX}{self.fact_id}",
            content="old value", embedding=[1.0, 0.0],
        )

    def test_guarded_edit_invalidates_index_and_rejects_stale_edit(self) -> None:
        revision = fact_revision(self.store.get_fact("alice", self.fact_id))
        self._index()
        edited = self._edit("new value", revision)
        self.assertEqual("new value", edited["value"])
        self.assertNotEqual(revision, fact_revision(edited))
        self.assertEqual([], self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE]))
        self._index()
        with self.assertRaisesRegex(ValueError, "Fact changed since it was loaded"):
            self._edit("stale overwrite", revision)
        self.assertEqual("new value", self.store.get_fact("alice", self.fact_id)["value"])
        self.assertEqual(1, len(self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE])))

    def test_stale_delete_preserves_fact_and_index_then_current_delete_removes_both(self) -> None:
        old_revision = fact_revision(self.store.get_fact("alice", self.fact_id))
        edited = self._edit("new value", old_revision)
        self._index()
        with self.assertRaisesRegex(ValueError, "Fact changed since it was loaded"):
            self.store.delete_fact("alice", self.fact_id, expected_revision=old_revision)
        self.assertIsNotNone(self.store.get_fact("alice", self.fact_id))
        self.assertEqual(1, len(self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE])))
        self.assertTrue(self.store.delete_fact("alice", self.fact_id, expected_revision=fact_revision(edited)))
        self.assertIsNone(self.store.get_fact("alice", self.fact_id))
        self.assertEqual([], self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE]))

    def test_revision_mutations_do_not_cross_owner_scope(self) -> None:
        revision = fact_revision(self.store.get_fact("alice", self.fact_id))
        self._index()
        self.assertIsNone(self._edit("other owner", revision, user_id="bob"))
        self.assertFalse(self.store.delete_fact("bob", self.fact_id, expected_revision=revision))
        self.assertEqual("old value", self.store.get_fact("alice", self.fact_id)["value"])
        self.assertEqual(1, len(self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE])))

    def test_revision_detects_metadata_change_and_ignores_preview_enrichment(self) -> None:
        fact = self.store.get_fact("alice", self.fact_id)
        revision = fact_revision(fact)
        self.assertEqual(revision, fact_revision({**fact, "similarity": 0.9, "revision": "ignored"}))
        with self.database.session() as connection:
            connection.execute("UPDATE memory_facts SET source_ref = ? WHERE id = ?", ("changed source", self.fact_id))
            connection.commit()
        with self.assertRaisesRegex(ValueError, "Fact changed since it was loaded"):
            self._edit("overwrite metadata", revision)

    def test_counts_include_own_and_global_memory_only(self) -> None:
        self._add("bob")
        self._add("global")
        for user_id, source_type, source_ref in (
            ("alice", "knowledge_text_personal", "own"),
            ("global", "knowledge_text_global", "global"),
            ("bob", "knowledge_text_personal", "other"),
            ("alice", FACT_EMBEDDING_SOURCE_TYPE, "fact"),
        ):
            self.store.add_embedding(user_id=user_id, source_type=source_type, source_ref=source_ref,
                                     content="content", embedding=[1.0, 0.0])
        self.assertEqual({"facts": 2, "turns": 0, "knowledge": 2}, self.store.memory_counts("alice"))
        self.assertEqual(1, self.store.memory_counts("global")["facts"])


if __name__ == "__main__":
    unittest.main()

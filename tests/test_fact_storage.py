from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lumen.memory.fact_policy import (
    FACT_EMBEDDING_SOURCE_TYPE,
    FACT_EMBEDDING_SOURCE_PREFIX,
    MANDATORY_TAG,
    fact_content_hash,
    fact_embedding_text,
    is_mandatory_fact,
    is_prompt_fact,
)
from lumen.memory.store import MemoryStore
from lumen.storage.db import Database


class FactStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.directory.name) / "lumen.db"))
        self.database.init()
        self.store = MemoryStore(self.database.session)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _add(self, *, user_id: str = "alice", subject: str = "subject", value: str = "value", **kwargs: object) -> int:
        return self.store.add_fact(
            user_id=user_id,
            category="profile",
            subject=subject,
            predicate="is",
            value=value,
            confidence=0.9,
            importance=5,
            source_ref="test",
            **kwargs,
        )

    def test_embedding_search_requires_explicit_user_scope(self) -> None:
        for user_id in ("", "   "):
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                self.store.search_embeddings([1.0, 0.0], user_id=user_id)

    def test_keyset_pages_are_user_scoped_and_reach_beyond_500_rows(self) -> None:
        for index in range(601):
            self._add(subject=f"subject-{index}")
        self._add(user_id="bob", subject="other-user")

        rows: list[dict] = []
        after_id = 0
        while True:
            page = self.store.list_fact_page("alice", after_id=after_id, limit=100)
            if not page:
                break
            rows.extend(page)
            after_id = page[-1]["id"]

        self.assertEqual(601, len(rows))
        self.assertEqual(sorted(row["id"] for row in rows), [row["id"] for row in rows])
        self.assertTrue(all(row["user_id"] == "alice" for row in rows))
        self.assertEqual("subject-0", rows[0]["subject"])
        self.assertEqual("subject-600", rows[-1]["subject"])

    def test_inclusion_marker_union_and_revoke(self) -> None:
        fact_id = self._add(tags=["user-tag"], inclusion="mandatory")
        fact = self.store.get_fact("alice", fact_id)
        self.assertIsNotNone(fact)
        self.assertTrue(is_mandatory_fact(fact))
        self.assertEqual({"user-tag", MANDATORY_TAG}, set(fact["tags"]))

        self._add(tags=["second-tag"])
        fact = self.store.get_fact("alice", fact_id)
        self.assertEqual({"user-tag", "second-tag", MANDATORY_TAG}, set(fact["tags"]))

        self._add(tags=["third-tag"], inclusion="contextual")
        fact = self.store.get_fact("alice", fact_id)
        self.assertEqual({"user-tag", "second-tag", "third-tag"}, set(fact["tags"]))

    def test_fact_embedding_lifecycle_and_invalid_target(self) -> None:
        fact_id = self._add()
        self.store.add_embedding(
            user_id="alice",
            source_type=FACT_EMBEDDING_SOURCE_TYPE,
            source_ref=f"{FACT_EMBEDDING_SOURCE_PREFIX}{fact_id}",
            content="old",
            embedding=[1.0, 0.0],
        )
        self.store.update_fact(
            fact_id,
            user_id="alice",
            category="profile",
            subject="subject",
            predicate="is",
            value="changed",
            confidence=0.9,
            importance=5,
            source_ref="test",
        )
        self.assertEqual([], self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE]))

        fact_id = self._add(subject="classification")
        self.store.add_embedding(
            user_id="alice",
            source_type=FACT_EMBEDDING_SOURCE_TYPE,
            source_ref=f"{FACT_EMBEDDING_SOURCE_PREFIX}{fact_id}",
            content="classification",
            embedding=[1.0, 0.0],
        )
        self._add(subject="classification", inclusion="mandatory")
        self.assertEqual([], self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE]))

        fact_id = self._add(subject="to-delete")
        self.store.add_embedding(
            user_id="alice",
            source_type=FACT_EMBEDDING_SOURCE_TYPE,
            source_ref=f"{FACT_EMBEDDING_SOURCE_PREFIX}{fact_id}",
            content="delete me",
            embedding=[1.0, 0.0],
        )
        self.assertTrue(self.store.delete_fact("alice", fact_id))
        self.assertIsNone(self.store.get_fact("alice", fact_id))
        self.assertEqual([], self.store.list_embeddings(user_id="alice", source_types=[FACT_EMBEDDING_SOURCE_TYPE]))

        with self.assertRaises(ValueError):
            self._add(fact_id=999999)
        for invalid_id in (True, False, 0, -1, "1"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                self._add(fact_id=invalid_id)

        target_id = self._add(subject="target", value="original")
        with self.assertRaises(ValueError):
            self._add(fact_id=target_id, subject="different", value="must-not-write")
        self.assertEqual("original", self.store.get_fact("alice", target_id)["value"])

    def test_fact_policy_is_canonical_and_prompt_safe(self) -> None:
        fact = {
            "category": "preference",
            "subject": "current_user",
            "predicate": "prefers",
            "value": "ти",
            "tags": [MANDATORY_TAG],
            "user_id": "alice",
            "expires_at": None,
        }
        self.assertEqual("preference\ncurrent_user\nprefers\nти", fact_embedding_text(fact))
        self.assertEqual(fact_content_hash(fact), fact_content_hash({**fact, "tags": ["other"]}))
        self.assertTrue(is_prompt_fact(fact, current_user_id="alice"))
        self.assertTrue(is_prompt_fact({**fact, "user_id": "global"}, current_user_id="alice"))
        self.assertFalse(is_prompt_fact({**fact, "user_id": "bob"}, current_user_id="alice"))
        self.assertFalse(is_prompt_fact({**fact, "category": "secret"}))
        self.assertFalse(is_prompt_fact({**fact, "expires_at": "not-a-date"}))
        self.assertFalse(
            is_prompt_fact(
                {**fact, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
            )
        )


if __name__ == "__main__":
    unittest.main()

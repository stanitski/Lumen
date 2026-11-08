from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lumen.memory.store import MemoryStore
from lumen.services.knowledge import KnowledgeService
from lumen.storage.db import Database


class KnowledgeReplacementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(str(Path(self.tmp.name) / "knowledge.db"))
        self.database.init()
        self.store = MemoryStore(self.database.session)
        self.ollama = SimpleNamespace(embed=AsyncMock())
        self.service = KnowledgeService(
            self.store, self.ollama, SimpleNamespace(ollama_embedding_model="test-model"),
            default_user_id="reader",
        )
        for user, source_type, ref in (
            ("reader", "knowledge_text_personal", "knowledge:personal:text:guide"),
            ("other", "knowledge_text_personal", "knowledge:personal:text:guide"),
            ("reader", "memory_fact", "memory:fact:1"),
        ):
            for index in range(2):
                self.store.add_embedding(
                    user_id=user, source_type=source_type, source_ref=ref,
                    chunk_index=index, content=f"old-{index}", embedding=[1.0, 0.0],
                )

    def rows(self) -> list[tuple]:
        with self.database.session() as connection:
            return [tuple(row) for row in connection.execute("SELECT * FROM memory_embeddings ORDER BY id")]

    async def upload(self, text: str = "replacement"):
        return await self.service.upload_bytes(
            filename="guide.txt", content_type="text/plain", raw=text.encode(),
            scope="personal", ingest_user_id=None, source_ref="guide", max_chunk_chars=500,
        )

    async def test_model_failure_keeps_previous_document(self) -> None:
        before = self.rows()
        self.ollama.embed.side_effect = RuntimeError("model unavailable")
        with self.assertRaises(RuntimeError):
            await self.upload()
        self.assertEqual(before, self.rows())

    async def test_cancellation_keeps_previous_document(self) -> None:
        before = self.rows()
        self.ollama.embed.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.upload()
        self.assertEqual(before, self.rows())

    async def test_invalid_batch_keeps_previous_document(self) -> None:
        for payload in (
            {}, {"embeddings": [[1.0, 0.0]]},
            {"embeddings": [[1.0, 0.0], []]},
            {"embeddings": [[1.0, 0.0], ["invalid", 0.0]]},
            {"embeddings": [[1.0, 0.0], [float("nan"), 0.0]]},
            {"embeddings": [[1.0, 0.0], [float("inf"), 0.0]]},
            {"embeddings": [[1.0, 0.0], [0.0, 0.0]]},
            {"embeddings": [[1.0, 0.0], [1.0]]},
        ):
            with self.subTest(payload=payload):
                before = self.rows()
                self.ollama.embed.return_value = payload
                with self.assertRaises(RuntimeError):
                    await self.upload("a" * 501)
                self.assertEqual(before, self.rows())

    async def test_success_replaces_only_document_and_removes_old_tail(self) -> None:
        before = self.rows()
        self.ollama.embed.return_value = {"embeddings": [[0.0, 1.0]]}
        result = await self.upload()
        self.assertEqual("indexed", result.status)
        self.assertEqual(1, result.embeddings_created)
        self.assertEqual("reader", result.user_id)
        rows = self.rows()
        untouched = [row for row in before if row[1] != "reader" or row[2] != "knowledge_text_personal"]
        self.assertEqual(untouched, [row for row in rows if row[1] != "reader" or row[2] != "knowledge_text_personal"])
        replaced = [row for row in rows if row[1] == "reader" and row[2] == "knowledge_text_personal"]
        self.assertEqual(1, len(replaced))
        self.assertEqual("replacement", replaced[0][5])

    async def test_database_failure_rolls_back_complete_replacement(self) -> None:
        before = self.rows()
        with self.database.session() as connection:
            connection.execute("""
                CREATE TRIGGER reject_second_new_chunk BEFORE INSERT ON memory_embeddings
                WHEN NEW.content = 'b'
                BEGIN SELECT RAISE(ABORT, 'simulated disk write failure'); END
            """)
            connection.commit()
        self.ollama.embed.return_value = {"embeddings": [[1.0, 0.0], [0.0, 1.0]]}
        with self.assertRaises(sqlite3.IntegrityError):
            await self.upload("a" * 500 + "b")
        self.assertEqual(before, self.rows())


if __name__ == "__main__":
    unittest.main()

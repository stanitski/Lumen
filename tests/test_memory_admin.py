"""Admin API behavior against isolated storage; no live integrations."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from tests.runtime_test_support import main


class MemoryAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.user_id = main.settings.lumen_user_id
        self.created: list[tuple[str, int]] = []
        self.transport = httpx.ASGITransport(app=main.app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for user_id, fact_id in self.created:
            main.memory_store.delete_fact(user_id, fact_id)

    def add_fact(self, *, user_id: str | None = None, category: str = "relationship", **extra: object) -> int:
        owner = self.user_id if user_id is None else user_id
        fact_id = main.memory_store.add_fact(
            user_id=owner,
            category=category,
            subject=f"admin-test-{uuid4().hex}",
            predicate="is",
            value="a separate contact",
            confidence=0.87,
            importance=7,
            source_ref="dialogue:turn:999999999",
            tags=["contact"],
            **extra,
        )
        self.created.append((owner, fact_id))
        return fact_id

    async def details(self, fact_id: int) -> dict:
        response = await self.client.get(f"/memory/admin/facts/{fact_id}")
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["fact"]

    @staticmethod
    def update_payload(fact: dict, value: str = "corrected contact") -> dict:
        return {
            "revision": fact["revision"],
            "category": fact["category"],
            "subject": fact["subject"],
            "predicate": fact["predicate"],
            "value": value,
            "inclusion": "contextual",
        }

    def index_result(self, *, failed: int = 0) -> AsyncMock:
        return AsyncMock(return_value={"indexed": 0 if failed else 1, "current": 0,
                                       "failed": failed, "removed": 0, "stale_race": 0})

    async def test_page_and_items_show_owned_facts_but_hide_other_users_and_credentials(self) -> None:
        owned_id = self.add_fact()
        other_id = self.add_fact(user_id="unrelated-admin-user")
        secret_id = self.add_fact(category="credential")
        response = await self.client.get("/memory")
        self.assertEqual(200, response.status_code)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("no-store", response.headers.get("cache-control", ""))
        self.assertIn("frame-ancestors 'none'", response.headers.get("content-security-policy", ""))
        response = await self.client.get("/memory/admin/items")
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        ids = {fact["id"] for fact in data["facts"]}
        self.assertIn(owned_id, ids)
        self.assertNotIn(other_id, ids)
        self.assertNotIn(secret_id, ids)
        self.assertEqual(self.user_id, data["user_id"])
        self.assertTrue(all(key in data["counts"] for key in ("facts", "turns", "knowledge")))

    async def test_edit_preserves_metadata_and_rejects_stale_edit_or_delete(self) -> None:
        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        fact_id = self.add_fact(expires_at=expiry)
        original = await self.details(fact_id)
        with patch.object(main.memory_admin_service._fact_context, "sync_index", self.index_result()):
            response = await self.client.patch(f"/memory/admin/facts/{fact_id}",
                                               json=self.update_payload(original))
        self.assertEqual(200, response.status_code, response.text)
        updated = response.json()["fact"]
        self.assertEqual("corrected contact", updated["value"])
        self.assertNotEqual(original["revision"], updated["revision"])
        stored = main.memory_store.get_fact(self.user_id, fact_id)
        for field in ("source_ref", "confidence", "importance", "expires_at", "tags"):
            self.assertEqual(original[field], stored[field], field)
        response = await self.client.patch(f"/memory/admin/facts/{fact_id}",
                                           json=self.update_payload(original, "must not overwrite"))
        self.assertEqual(409, response.status_code, response.text)
        response = await self.client.request("DELETE", f"/memory/admin/facts/{fact_id}",
                                             json={"revision": original["revision"]})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("corrected contact", main.memory_store.get_fact(self.user_id, fact_id)["value"])

    async def test_failed_index_is_explicit_and_stale_embedding_is_removed(self) -> None:
        fact_id = self.add_fact()
        main.memory_store.add_embedding(user_id=self.user_id, source_type="memory_fact",
            source_ref=f"memory:fact:{fact_id}", content="outdated fact", embedding=[1.0, 0.0])
        original = await self.details(fact_id)
        with patch.object(main.memory_admin_service._fact_context, "sync_index", self.index_result(failed=1)):
            response = await self.client.patch(f"/memory/admin/facts/{fact_id}",
                                               json=self.update_payload(original))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("failed", response.json()["index"]["status"])
        self.assertEqual("corrected contact", main.memory_store.get_fact(self.user_id, fact_id)["value"])
        refs = {hit.source_ref for hit in main.memory_store.list_embeddings(
            user_id=self.user_id, source_types=["memory_fact"], limit=None)}
        self.assertNotIn(f"memory:fact:{fact_id}", refs)

    async def test_delete_removes_exact_fact_and_index_only(self) -> None:
        fact_id, survivor_id = self.add_fact(), self.add_fact()
        main.memory_store.add_embedding(user_id=self.user_id, source_type="memory_fact",
            source_ref=f"memory:fact:{fact_id}", content="delete target", embedding=[1.0, 0.0])
        fact = await self.details(fact_id)
        response = await self.client.request("DELETE", f"/memory/admin/facts/{fact_id}",
                                             json={"revision": fact["revision"]})
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()["deleted"])
        self.assertIsNone(main.memory_store.get_fact(self.user_id, fact_id))
        self.assertIsNotNone(main.memory_store.get_fact(self.user_id, survivor_id))
        refs = {hit.source_ref for hit in main.memory_store.list_embeddings(
            user_id=self.user_id, source_types=["memory_fact"], limit=None)}
        self.assertNotIn(f"memory:fact:{fact_id}", refs)

    async def test_other_owner_global_and_secret_cannot_be_edited(self) -> None:
        for owner, category in (("unrelated-admin-user", "profile"), ("global", "profile"),
                                (self.user_id, "credential")):
            fact_id = self.add_fact(user_id=owner, category=category)
            raw = main.memory_store.get_fact(owner, fact_id)
            payload = {"revision": "0" * 64, "category": "profile", "subject": raw["subject"],
                       "predicate": "is", "value": "must not write", "inclusion": "contextual"}
            response = await self.client.patch(f"/memory/admin/facts/{fact_id}", json=payload)
            self.assertEqual(404, response.status_code, response.text)
            self.assertEqual(raw["value"], main.memory_store.get_fact(owner, fact_id)["value"])

    async def test_cross_origin_and_nonlocal_requests_are_rejected(self) -> None:
        fact_id = self.add_fact()
        fact = await self.details(fact_id)
        response = await self.client.request("DELETE", f"/memory/admin/facts/{fact_id}",
            json={"revision": fact["revision"]}, headers={"Origin": "https://unrelated.example"})
        self.assertEqual(403, response.status_code, response.text)
        response = await self.client.request("DELETE", f"/memory/admin/facts/{fact_id}",
            json={"revision": fact["revision"]}, headers={"Origin": "null"})
        self.assertEqual(403, response.status_code, response.text)
        remote = httpx.ASGITransport(app=main.app, client=("192.0.2.20", 1234))
        async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1") as client:
            response = await client.get("/memory/admin/items")
        self.assertEqual(403, response.status_code, response.text)
        response = await self.client.get("/memory/admin/items", headers={"Host": "unrelated.example"})
        self.assertEqual(403, response.status_code, response.text)
        self.assertIsNotNone(main.memory_store.get_fact(self.user_id, fact_id))

    async def test_preview_preserves_exact_fact_snapshot_and_never_persists_or_delivers(self) -> None:
        fact_id = self.add_fact()
        original = await self.details(fact_id)
        snapshot = copy.deepcopy(main.memory_store.get_fact(self.user_id, fact_id))
        with patch.object(main.memory_admin_service._fact_context, "sync_index", self.index_result()):
            response = await self.client.patch(f"/memory/admin/facts/{fact_id}",
                                               json=self.update_payload(original, "new live value"))
        self.assertEqual(200, response.status_code, response.text)
        context = {"selected_facts": [snapshot], "mandatory_facts": [], "contextual_facts": [snapshot],
                   "mandatory_context": "", "memory_context": "exact original fact text",
                   "dialogue_context": "", "knowledge_context": "", "knowledge_hits": [],
                   "dialogue_hits": [], "previous_turns": [], "context_execution": []}
        messages = [{"role": "system", "content": "exact original fact text"},
                    {"role": "user", "content": "Хто це?"}]
        with patch.object(main.memory_admin_service._chat_service, "_build_dialogue_messages",
                          AsyncMock(return_value=(messages, context))) as build, \
             patch.object(main.memory_admin_service._chat_service._ollama, "chat",
                          AsyncMock(return_value={"message": {"content": "Це контакт."}})) as model, \
             patch.object(main.conversation_service, "finish", AsyncMock()) as finish, \
             patch.object(main.delivery_service, "deliver_chat_answer", AsyncMock()) as deliver:
            response = await self.client.post("/memory/admin/preview", json={"text": "Хто це?", "generate_answer": True})
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual("Це контакт.", data["answer"])
        self.assertEqual(original["value"], data["context"]["selected_facts"][0]["value"])
        self.assertEqual(original["revision"], data["context"]["selected_facts"][0]["revision"])
        self.assertEqual(len(messages), data["message_count"])
        self.assertEqual(messages, model.call_args.kwargs["messages"])
        self.assertEqual(self.user_id, build.call_args.kwargs["user_id"])
        finish.assert_not_awaited()
        deliver.assert_not_awaited()

    async def test_search_without_answer_skips_final_model_call(self) -> None:
        messages = [{"role": "user", "content": "contact"}]
        context = {"selected_facts": [], "mandatory_facts": [], "contextual_facts": [],
                   "dialogue_hits": [], "knowledge_hits": [], "previous_turns": [],
                   "memory_context": "", "dialogue_context": "", "knowledge_context": ""}
        with patch.object(main.memory_admin_service._chat_service, "_build_dialogue_messages",
                          AsyncMock(return_value=(messages, context))), \
             patch.object(main.memory_admin_service._chat_service._ollama, "chat", AsyncMock()) as model:
            response = await self.client.post("/memory/admin/preview", json={"text": "contact", "generate_answer": False})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(response.json()["answer"])
        model.assert_not_awaited()

    async def test_invalid_preview_and_empty_fact_value_are_rejected_before_model_work(self) -> None:
        fact_id = self.add_fact()
        fact = await self.details(fact_id)
        with patch.object(main.memory_admin_service._chat_service, "_build_dialogue_messages", AsyncMock()) as build:
            response = await self.client.post("/memory/admin/preview", json={"text": "   ", "generate_answer": True})
        self.assertEqual(422, response.status_code, response.text)
        build.assert_not_awaited()
        response = await self.client.patch(f"/memory/admin/facts/{fact_id}",
                                           json=self.update_payload(fact, "   "))
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual(fact["value"], main.memory_store.get_fact(self.user_id, fact_id)["value"])

    async def test_preview_model_failure_is_not_reported_as_success(self) -> None:
        with patch.object(main.memory_admin_service._chat_service, "_build_dialogue_messages",
                          AsyncMock(return_value=([{"role": "user", "content": "test"}], {}))), \
             patch.object(main.memory_admin_service._chat_service._ollama, "chat",
                          AsyncMock(side_effect=TimeoutError("test timeout"))), \
             patch.object(main.conversation_service, "finish", AsyncMock()) as finish:
            response = await self.client.post("/memory/admin/preview", json={"text": "test", "generate_answer": True})
        self.assertEqual(502, response.status_code, response.text)
        self.assertNotIn("answer", response.json())
        finish.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

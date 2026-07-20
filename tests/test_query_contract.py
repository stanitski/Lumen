from __future__ import annotations

import unittest
import json

from lumen.config import Settings
from lumen.services.memory import MemoryService


class FakeOllama:
    async def chat(self, **kwargs):
        self.kwargs = kwargs
        return {
            "message": {
                "content": (
                    '{"topic":"research","answer_mode":"find","time_scope":"none","completeness":"normal",'
                    '"context_sources":[{"source":"telegram","operation":"search","query":"Jan","scope":"approved_chats","source_kind":"user","date_from":"2026-07-15","date_to":"2026-07-15"},'
                    '{"source":"web","operation":"search","query":"latest news","scope":"default"},'
                    '{"source":"home_assistant","operation":"read_state","query":"kitchen light","scope":"default"},'
                    '{"source":"knowledge","operation":"search","query":"project notes","scope":"default"},'
                    '{"source":"telegram","operation":"send","query":"bad","scope":"approved_chats"}]}'
                )
            }
        }


class QueryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract_returns_only_allowed_context_sources(self) -> None:
        service = MemoryService(object(), FakeOllama(), Settings())

        contract = await service.get_contract("Find information")

        self.assertEqual(
            ["telegram", "web", "home_assistant", "knowledge"],
            [item["source"] for item in contract["context_sources"]],
        )
        self.assertEqual("search", contract["context_sources"][0]["operation"])
        self.assertEqual("user", contract["context_sources"][0]["source_kind"])
        self.assertEqual("2026-07-15", contract["context_sources"][0]["date_from"])

    async def test_contract_receives_approved_telegram_sources_as_routing_context(self) -> None:
        class SourceStore:
            def list_facts(self, user_id, *, limit, include_global):
                return [{
                    "category": "messenger_context",
                    "predicate": "uses",
                    "tags": ["telegram", "search_source"],
                    "value": json.dumps({"title": "Україна Online", "ref": "@UaOnlii", "kind": "channel"}),
                }]

        ollama = FakeOllama()
        service = MemoryService(SourceStore(), ollama, Settings())

        routing_context = service._routing_context_for_contract("VST")
        await service.get_contract("Переглянь телеграм канали", routing_context=routing_context)

        prompt = "\n".join(message["content"] for message in ollama.kwargs["messages"])
        self.assertIn("Available approved Telegram data sources", prompt)
        self.assertIn("kind=channel | title=Україна Online | ref=@UaOnlii", prompt)

    def test_invalid_context_sources_are_discarded(self) -> None:
        self.assertEqual([], MemoryService._normalize_context_sources([{"source": "telegram", "operation": "send"}], fallback_query="test"))


if __name__ == "__main__":
    unittest.main()

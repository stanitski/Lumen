from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from lumen.config import Settings
from lumen.models import ChatRequest, ConversationTurnRecord, EmbeddingHit
from lumen.services.chat_only import ChatOnlyService
from lumen.services.context_executor import ContextExecutionResult
from lumen.services.memory import MemoryService


class FakeOllama:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def chat(self, *, messages: list[dict[str, str]], model: str | None = None) -> dict[str, object]:
        del model
        self.messages = messages
        return {"message": {"content": "Відповідь з діалогового режиму."}}


class FakeMemoryService:
    def __init__(self) -> None:
        self.context_options: dict[str, object] = {}
        self.stored_turn: dict[str, object] = {}
        self.stored_facts: list[dict[str, object]] = []
        self.postprocessed_turn_ids: list[int] = []
        self.retrieval_policy: dict[str, bool] | None = None
        self.query_contract: dict[str, object] | None = None

    def resolve_user_id(self, payload: ChatRequest) -> str:
        return payload.user_id or "VST"

    def should_enable_voice_response(self, text: str, user_id: str) -> bool:
        del text, user_id
        return False

    async def build_planner_context(self, **kwargs: object) -> dict[str, object]:
        self.context_options = kwargs
        context = {
            "memory_context": "Memory:\n<none>",
            "dialogue_context": "Dialogue:\n<none>",
            "knowledge_context": "Knowledge:\n<none>",
            "recent_turns": [],
            "selected_facts": [],
            "dialogue_hits": [],
        }
        if self.retrieval_policy is not None:
            context["retrieval_policy"] = self.retrieval_policy
        if self.query_contract is not None:
            context["query_contract"] = self.query_contract
        return context

    def recent_dialogue_turns(self, *, user_id: str, limit: int = 6) -> list[object]:
        del user_id, limit
        return []

    def classify_turn_retention(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"memory_class": "dialogue_personal", "retention_policy": "permanent", "expires_at": None}

    def classify_turn_topics(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"topics": ["діалог"], "entities": [], "tags": ["діалог"], "continues_previous_topic": False}

    def add_turn(self, **kwargs: object) -> int:
        self.stored_turn = kwargs
        return 7

    async def recover_unprocessed_dialogue_turns(self, *, user_id: str) -> int:
        del user_id
        return 0

    async def postprocess_dialogue_turn(self, *, user_id: str, turn_id: int) -> None:
        del user_id
        self.postprocessed_turn_ids.append(turn_id)
        self.stored_facts = [{"category": "preference"}]

    async def extract_memory_facts(self, text: str, user_id: str) -> list[dict[str, object]]:
        del text, user_id
        return [{"category": "preference", "subject": "current_user", "predicate": "prefers", "value": "короткі відповіді"}]

    def store_memory_facts(self, facts: list[dict[str, object]], **kwargs: object) -> None:
        del kwargs
        self.stored_facts = facts


class FakeDeliveryService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def deliver_chat_answer(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class FakeContextExecutor:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def execute(self, requests: object, *, user_id: str, user_text: str) -> list[ContextExecutionResult]:
        self.calls.append((requests, user_id, user_text))
        return [ContextExecutionResult("telegram", "search", "pending_implementation", "No external context was retrieved.")]

    @staticmethod
    def format_for_prompt(results: list[ContextExecutionResult]) -> str:
        del results
        return "External context requested but unavailable. Do not claim that you searched it."


class ChatOnlyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_executes_contract_context_sources_before_answer(self) -> None:
        memory = FakeMemoryService()
        memory.query_contract = {
            "topic": "person",
            "answer_mode": "find",
            "time_scope": "none",
            "completeness": "normal",
            "context_sources": [{"source": "telegram", "operation": "search", "query": "Jan", "scope": "approved_chats"}],
        }
        executor = FakeContextExecutor()
        ollama = FakeOllama()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=FakeDeliveryService(),
            settings=Settings(),
            context_executor=executor,
        )

        await service.process_chat_request_result(ChatRequest(text="Find Jan", user_id="test-user"), "request-context", deliver=False)

        self.assertEqual("test-user", executor.calls[0][1])
        self.assertTrue(any("Do not claim" in message["content"] for message in ollama.messages))
    async def test_chat_only_uses_dialogue_memory_without_knowledge(self) -> None:
        memory = FakeMemoryService()
        ollama = FakeOllama()
        delivery = FakeDeliveryService()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=delivery,
            settings=Settings(),
        )

        result = await service.process_chat_request_result(
            ChatRequest(text="Привіт", user_id="test-user"),
            "request-1",
            deliver=False,
        )

        self.assertEqual("Відповідь з діалогового режиму.", result.answer)
        self.assertEqual(True, memory.context_options["include_knowledge"])
        self.assertEqual(False, memory.context_options["include_recent_turns"])
        self.assertEqual(False, memory.context_options["include_topic_expansion"])
        self.assertEqual(True, memory.context_options["force_semantic_dialogue"])
        self.assertEqual("chat", memory.stored_turn["turn_meta"]["kind"])
        self.assertEqual("inline", result.delivery_outcome)
        self.assertEqual([], delivery.calls)
        self.assertEqual([7], memory.postprocessed_turn_ids)
        self.assertFalse(any("Skill:" in item["content"] for item in ollama.messages))

    def test_dialogue_retrieval_applies_time_decay_after_similarity_filter(self) -> None:
        now = datetime.now().astimezone()
        fresh = EmbeddingHit(
            id=1,
            user_id="test-user",
            source_type="dialogue_turn",
            source_ref="dialogue:turn:1",
            chunk_index=0,
            content="Fresh relevant dialogue",
            importance=5,
            similarity=0.70,
            created_at=now.isoformat(),
        )
        old = EmbeddingHit(
            id=2,
            user_id="test-user",
            source_type="dialogue_turn",
            source_ref="dialogue:turn:2",
            chunk_index=0,
            content="Old relevant dialogue",
            importance=5,
            similarity=0.75,
            created_at=(now - timedelta(days=30)).isoformat(),
        )
        weak = EmbeddingHit(
            id=3,
            user_id="test-user",
            source_type="dialogue_turn",
            source_ref="dialogue:turn:3",
            chunk_index=0,
            content="Weak dialogue",
            importance=5,
            similarity=0.40,
            created_at=now.isoformat(),
        )

        selected = MemoryService._select_dialogue_hits([old, weak, fresh])

        self.assertEqual(["dialogue:turn:1", "dialogue:turn:2"], [hit.source_ref for hit in selected])
        self.assertAlmostEqual(0.82, MemoryService.dialogue_retrieval_weight(fresh, now=now))

    async def test_chat_only_preserves_inline_system_context(self) -> None:
        ollama = FakeOllama()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=FakeMemoryService(),
            delivery_service=FakeDeliveryService(),
            settings=Settings(),
        )

        await service.process_chat_request_result(
            ChatRequest(text="Поясни коротко"),
            "request-3",
            supplemental_context="Відповідай одним реченням.",
            deliver=False,
        )

        self.assertIn("Відповідай одним реченням.", [item["content"] for item in ollama.messages])

    async def test_memory_debug_search_uses_chat_context_without_persisting(self) -> None:
        memory = FakeMemoryService()
        ollama = FakeOllama()
        delivery = FakeDeliveryService()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=delivery,
            settings=Settings(),
        )

        result = await service.debug_memory_search(user_text="Test query", user_id="test-user")

        self.assertTrue(result["answer"])
        self.assertEqual(5, result["message_count"])
        self.assertEqual(True, memory.context_options["force_semantic_dialogue"])
        self.assertEqual([], result["context"]["previous_turns"])
        self.assertEqual({}, memory.stored_turn)
        self.assertEqual([], delivery.calls)

    async def test_chat_only_always_includes_one_previous_turn(self) -> None:
        memory = FakeMemoryService()
        memory.recent_dialogue_turns = lambda **kwargs: [
            ConversationTurnRecord(
                id=6,
                user_id="test-user",
                source="learning-api",
                question="Previous question",
                answer="Previous answer",
                question_created_at="2026-07-16T10:00:00+03:00",
                answer_created_at="2026-07-16T10:00:01+03:00",
                created_at="2026-07-16T10:00:01+03:00",
                memory_processed=1,
            )
        ]
        ollama = FakeOllama()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=FakeDeliveryService(),
            settings=Settings(),
        )

        await service.process_chat_request_result(
            ChatRequest(text="Why?", user_id="test-user"),
            "request-previous-turn",
            deliver=False,
        )

        messages = [(item["role"], item["content"]) for item in ollama.messages]
        self.assertIn(("user", "Previous question"), messages)
        self.assertIn(("assistant", "Previous answer"), messages)
        self.assertLess(messages.index(("assistant", "Previous answer")), messages.index(("user", "Why?")))

    async def test_chat_only_omits_previous_turn_when_policy_disables_it(self) -> None:
        memory = FakeMemoryService()
        memory.retrieval_policy = {"include_previous_turn": False}
        memory.recent_dialogue_turns = lambda **kwargs: [
            ConversationTurnRecord(
                id=6,
                user_id="test-user",
                source="learning-api",
                question="Previous question",
                answer="Previous answer",
                question_created_at="2026-07-16T10:00:00+03:00",
                answer_created_at="2026-07-16T10:00:01+03:00",
                created_at="2026-07-16T10:00:01+03:00",
                memory_processed=1,
            )
        ]
        ollama = FakeOllama()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=FakeDeliveryService(),
            settings=Settings(),
        )

        await service.debug_memory_search(user_text="Hi", user_id="test-user")

        messages = [(item["role"], item["content"]) for item in ollama.messages]
        self.assertNotIn(("user", "Previous question"), messages)

    async def test_chat_only_delivers_after_persisting(self) -> None:
        memory = FakeMemoryService()
        delivery = FakeDeliveryService()
        service = ChatOnlyService(
            ollama=FakeOllama(),
            memory_service=memory,
            delivery_service=delivery,
            settings=Settings(),
        )

        result = await service.process_chat_request_result(ChatRequest(text="Як справи?"), "request-2")

        self.assertEqual("stored", result.persistence_outcome)
        self.assertTrue(result.delivered)
        self.assertEqual([7], memory.postprocessed_turn_ids)
        self.assertEqual("Відповідь з діалогового режиму.", delivery.calls[0]["answer"])
        self.assertEqual(memory.stored_facts[0]["category"], "preference")


if __name__ == "__main__":
    unittest.main()

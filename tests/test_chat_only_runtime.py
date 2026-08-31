from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

from lumen.config import Settings
from lumen.connectors.ollama import OllamaStreamIdleTimeout, OllamaStreamProtocolError
from lumen.models import ChatRequest, ConversationTurnRecord, EmbeddingHit
from lumen.services.chat_only import ChatOnlyService
from lumen.services.calendar_actions import CalendarActionResult, CalendarCreateRequest
from lumen.services.context_executor import ContextExecutionResult
from lumen.services.delivery import DeliveryService
from lumen.services.memory import MemoryService
from lumen.services.reminders import ReminderInterception


class FakeOllama:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.stream_idle_timeout_seconds: float | None = None

    async def chat(self, *, messages: list[dict[str, str]], model: str | None = None) -> dict[str, object]:
        del model
        self.messages = messages
        return {"message": {"content": "Відповідь з діалогового режиму."}}

    async def stream_chat(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        idle_timeout_seconds: float,
        request_id: str = "",
    ):
        del model, request_id
        self.messages = messages
        self.stream_idle_timeout_seconds = idle_timeout_seconds
        yield "Відповідь з діалогового режиму."


class IdleStreamOllama(FakeOllama):
    async def stream_chat(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        idle_timeout_seconds: float,
        request_id: str = "",
    ):
        del model, request_id
        self.messages = messages
        self.stream_idle_timeout_seconds = idle_timeout_seconds
        yield "Часткова відповідь, яку не треба доставляти."
        raise OllamaStreamIdleTimeout("Ollama stream made no progress")


class ConcurrentStreamOllama(FakeOllama):
    def __init__(self) -> None:
        super().__init__()
        self.started = 0
        self.both_started = asyncio.Event()

    async def stream_chat(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        idle_timeout_seconds: float,
        request_id: str = "",
    ):
        del messages, model, idle_timeout_seconds
        self.started += 1
        if self.started == 2:
            self.both_started.set()
        await self.both_started.wait()
        yield f"Відповідь {request_id}"


class ProtocolErrorStreamOllama(FakeOllama):
    async def stream_chat(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        idle_timeout_seconds: float,
        request_id: str = "",
    ):
        del messages, model, idle_timeout_seconds, request_id
        if False:
            yield ""
        raise OllamaStreamProtocolError("missing done frame")


class FakeMemoryService:
    def __init__(self) -> None:
        self.context_options: dict[str, object] = {}
        self.stored_turn: dict[str, object] = {}
        self.stored_facts: list[dict[str, object]] = []
        self.postprocessed_turn_ids: list[int] = []
        self.retrieval_policy: dict[str, bool] | None = None
        self.query_contract: dict[str, object] | None = None
        self.contract_calls = 0
        self.context_contract_calls = 0
        self.raise_contract = False
        self.raise_context = False

    def resolve_user_id(self, payload: ChatRequest) -> str:
        return payload.user_id or "VST"

    def should_enable_voice_response(self, text: str, user_id: str) -> bool:
        del text, user_id
        return False

    async def get_contract_for_user(self, text: str, *, user_id: str) -> dict[str, object]:
        del text, user_id
        self.contract_calls += 1
        if self.raise_contract:
            raise TimeoutError("contract timeout")
        return self.query_contract or self._default_query_contract()

    @staticmethod
    def _default_query_contract() -> dict[str, object]:
        return {
            "topic": "", "answer_mode": "direct_answer", "time_scope": "none", "completeness": "normal",
            "context_sources": [], "reminder": {"action": "none", "items": [], "delete_ordinal": None, "confirmation": ""},
        }

    async def build_planner_context(self, **kwargs: object) -> dict[str, object]:
        if self.raise_context:
            raise RuntimeError("context build failed")
        self.context_options = kwargs
        if "query_contract" not in kwargs:
            self.context_contract_calls += 1
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
        supplied_contract = kwargs.get("query_contract")
        context["query_contract"] = supplied_contract if isinstance(supplied_contract, dict) else self.query_contract or self._default_query_contract()
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


class FakeReminderService:
    def __init__(self) -> None:
        self.contracts: list[dict[str, object]] = []
        self.metadata: list[dict[str, object]] = []
        self.delivery_routes: list[dict[str, object] | None] = []
        self.result: ReminderInterception | None = None

    def intercept_contract(self, *, contract: dict[str, object], **kwargs: object) -> ReminderInterception | None:
        self.contracts.append(contract)
        self.metadata.append(kwargs.get("metadata", {}))
        self.delivery_routes.append(kwargs.get("delivery_route"))
        return self.result


class FakeCalendarActionService:
    def __init__(self, result: CalendarActionResult) -> None:
        self.result = result
        self.contracts: list[dict[str, object]] = []

    async def execute(self, contract: dict[str, object]) -> CalendarActionResult:
        self.contracts.append(contract)
        return self.result


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
    async def test_chat_voice_policy_reaches_result_and_callback(self) -> None:
        cases = (
            ("Коротко поясни причину тривоги і відповідай вголос.", [], True),
            ("Відповідай ВГОЛОС.", [], True),
            ("Ответь вслух.", [], True),
            ("Коротко поясни причину тривоги.", [], False),
            ("Привіт", [{"id": 1, "value": "Відповідати вголос"}], True),
        )
        for text, facts, expected_voice in cases:
            for deliver in (True, False):
                with self.subTest(text=text, deliver=deliver):
                    store = Mock()
                    store.list_facts.return_value = facts
                    settings = Settings()
                    ollama = FakeOllama()
                    policy = MemoryService(store=store, ollama=ollama, settings=settings)
                    memory = FakeMemoryService()
                    memory.should_enable_voice_response = policy.should_enable_voice_response
                    delivery = DeliveryService(settings)
                    delivery.post_home_assistant_callback = AsyncMock()
                    service = ChatOnlyService(
                        ollama=ollama, memory_service=memory,
                        delivery_service=delivery, settings=settings,
                    )
                    route = {"source": "home_assistant"}

                    result = await service.process_chat_request_result(
                        ChatRequest(text=text, user_id="test-user", metadata={"skip_memory": True}),
                        "voice-regression", deliver=deliver, callback_metadata=route,
                    )

                    self.assertEqual(expected_voice, result.voice)
                    self.assertEqual(deliver, result.delivered)
                    self.assertEqual("skipped", result.persistence_outcome)
                    self.assertEqual({}, memory.stored_turn)
                    if deliver:
                        delivery.post_home_assistant_callback.assert_awaited_once()
                        callback = delivery.post_home_assistant_callback.await_args.args[0]
                        self.assertEqual(expected_voice, callback.voice)
                        self.assertEqual(result.answer, callback.answer)
                        self.assertEqual("voice-regression", callback.request_id)
                        self.assertEqual("home_assistant", callback.metadata["source"])
                    else:
                        delivery.post_home_assistant_callback.assert_not_awaited()

    def test_reminder_route_allowlist_rejects_invalid_values(self) -> None:
        self.assertEqual(
            {"target_channel": "telegram", "target_chat_id": "-1001234567890"},
            ChatOnlyService._reminder_delivery_route({"target_channel": "telegram", "target_chat_id": "-1001234567890"}),
        )
        for route in (
            {"target_channel": "discord", "target_chat_id": "123"},
            {"target_channel": "telegram", "target_chat_id": "not-numeric"},
            {"target_channel": "telegram", "target_chat_id": "1" * 501},
        ):
            with self.subTest(route=route):
                self.assertIsNone(ChatOnlyService._reminder_delivery_route(route))
        self.assertEqual(
            {"target_channel": "telegram", "target_chat_id": "123"},
            ChatOnlyService._reminder_delivery_route(
                {"target_channel": "telegram", "target_chat_id": "123", "scope": "private"}
            ),
        )

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
        self.assertNotIn("query_contract", memory.context_options)
        self.assertEqual(1, memory.context_contract_calls)
        self.assertEqual("chat", memory.stored_turn["turn_meta"]["kind"])
        self.assertEqual("inline", result.delivery_outcome)
        self.assertEqual([], delivery.calls)
        self.assertEqual([7], memory.postprocessed_turn_ids)
        self.assertFalse(any("Skill:" in item["content"] for item in ollama.messages))
        self.assertEqual(0, memory.contract_calls)
        self.assertIn("Do not claim to perform actions, create reminders,", ollama.messages[0]["content"])
        self.assertNotIn("deterministic reminder service", ollama.messages[0]["content"])

    async def test_reminder_contract_short_circuits_final_ollama(self) -> None:
        memory = FakeMemoryService()
        memory.query_contract = {
            "topic": "medication_reminder",
            "answer_mode": "direct_answer",
            "time_scope": "none",
            "completeness": "normal",
            "context_sources": [],
            "reminder": {"action": "create", "items": [{"text": "take medicine"}], "delete_ordinal": None, "confirmation": ""},
        }
        reminder = FakeReminderService()
        reminder.result = ReminderInterception("Reminder recorded.")
        ollama = FakeOllama()
        service = ChatOnlyService(
            ollama=ollama, memory_service=memory, delivery_service=FakeDeliveryService(),
            settings=Settings(), reminder_service=reminder,
        )

        result = await service.process_chat_request_result(
            ChatRequest(text="Нагадай завтра о 09:00 воду", user_id="test-user"), "timeout", deliver=False,
        )

        self.assertEqual("Reminder recorded.", result.answer)
        self.assertEqual("skipped", result.persistence_outcome)
        self.assertEqual([], ollama.messages)
        self.assertEqual(1, len(reminder.contracts))
        self.assertEqual("create", reminder.contracts[0]["reminder"]["action"])

    async def test_calendar_create_contract_executes_once_and_short_circuits_final_ollama(self) -> None:
        memory = FakeMemoryService()
        memory.query_contract = {
            "topic": "calendar_event_creation",
            "answer_mode": "create_or_update",
            "time_scope": "explicit_period",
            "completeness": "normal",
            "context_sources": [],
            "reminder": {"action": "none", "items": [], "delete_ordinal": None, "confirmation": ""},
            "calendar": {
                "action": "create",
                "item": {
                    "summary": "Project meeting",
                    "description": "Discuss release",
                    "location": "",
                    "all_day": False,
                    "start": "2026-07-25T15:00:00+03:00",
                    "end": "",
                },
            },
        }
        request = CalendarCreateRequest(
            summary="Project meeting",
            all_day=False,
            start=datetime.fromisoformat("2026-07-25T15:00:00+03:00"),
            end=datetime.fromisoformat("2026-07-25T16:00:00+03:00"),
            description="Discuss release",
        )
        calendar = FakeCalendarActionService(CalendarActionResult("created", "created", request, True))
        ollama = FakeOllama()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=FakeDeliveryService(),
            settings=Settings(),
            calendar_action_service=calendar,
        )

        result = await service.process_chat_request_result(
            ChatRequest(text="Add a project meeting", user_id="test-user"),
            "calendar-create",
            deliver=False,
        )

        self.assertEqual("Подію додано до календаря: Project meeting, 25.07.2026 15:00–16:00.", result.answer)
        self.assertEqual("created", result.metadata["calendar_action_status"])
        self.assertEqual(1, len(calendar.contracts))
        self.assertEqual([], ollama.messages)

    async def test_calendar_unknown_result_is_not_reported_as_created(self) -> None:
        memory = FakeMemoryService()
        memory.query_contract = {
            **memory._default_query_contract(),
            "calendar": {"action": "create", "item": {"summary": "Meeting"}},
        }
        calendar = FakeCalendarActionService(CalendarActionResult("unknown", "unknown"))
        service = ChatOnlyService(
            ollama=FakeOllama(),
            memory_service=memory,
            delivery_service=FakeDeliveryService(),
            settings=Settings(),
            calendar_action_service=calendar,
        )

        result = await service.process_chat_request_result(
            ChatRequest(text="Add meeting", user_id="test-user"),
            "calendar-unknown",
            deliver=False,
        )

        self.assertIn("не створити дублікат", result.answer)
        self.assertEqual("error", result.execution_outcome)

    async def test_callback_metadata_route_is_passed_to_reminder_creation(self) -> None:
        memory = FakeMemoryService()
        memory.query_contract = {
            "topic": "medication_reminder",
            "answer_mode": "direct_answer",
            "time_scope": "none",
            "completeness": "normal",
            "context_sources": [],
            "reminder": {"action": "create", "items": [{"text": "take medicine"}], "delete_ordinal": None, "confirmation": ""},
        }
        reminder = FakeReminderService()
        reminder.result = ReminderInterception("Reminder recorded.")
        service = ChatOnlyService(
            ollama=FakeOllama(),
            memory_service=memory,
            delivery_service=FakeDeliveryService(),
            settings=Settings(),
            reminder_service=reminder,
        )

        await service.process_chat_request_result(
            ChatRequest(text="remind me", user_id="test-user"),
            "request-route",
            callback_metadata={"target_channel": "telegram", "target_chat_id": "492198718"},
            deliver=False,
        )

        self.assertEqual(
            {"target_channel": "telegram", "target_chat_id": "492198718"},
            reminder.delivery_routes[-1],
        )

    async def test_structured_confirmation_short_circuits_context_build(self) -> None:
        memory = FakeMemoryService()
        memory.raise_context = True
        reminder = FakeReminderService()
        reminder.result = ReminderInterception("Reminder confirmed.")
        ollama = FakeOllama()
        service = ChatOnlyService(
            ollama=ollama, memory_service=memory, delivery_service=FakeDeliveryService(),
            settings=Settings(), reminder_service=reminder,
        )

        result = await service.process_chat_request_result(
            ChatRequest(
                text="Yes",
                user_id="test-user",
                metadata={"reminder_id": 7, "delivery_id": "delivery-7"},
            ),
            "confirmation",
            deliver=False,
        )

        self.assertEqual("Reminder confirmed.", result.answer)
        self.assertEqual({}, memory.context_options)
        self.assertEqual([], ollama.messages)
        self.assertEqual([{}], reminder.contracts)
        self.assertEqual("delivery-7", reminder.metadata[0]["delivery_id"])

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
        self.assertEqual(6, result["message_count"])
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

    async def test_idle_dialogue_stream_discards_partial_answer_without_side_effects(self) -> None:
        memory = FakeMemoryService()
        delivery = FakeDeliveryService()
        ollama = IdleStreamOllama()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=delivery,
            settings=Settings(),
            ollama_stream_idle_timeout_seconds=12.0,
        )

        result = await service.process_chat_request_result(
            ChatRequest(text="Знайди важливе в каналах", user_id="test-user"),
            "idle-request",
        )

        self.assertEqual("", result.answer)
        self.assertEqual("cancelled_idle", result.execution_outcome)
        self.assertEqual("skipped", result.persistence_outcome)
        self.assertEqual("suppressed", result.delivery_outcome)
        self.assertEqual({"outcome": "cancelled_idle"}, result.metadata)
        self.assertEqual(12.0, ollama.stream_idle_timeout_seconds)
        self.assertEqual({}, memory.stored_turn)
        self.assertEqual([], memory.postprocessed_turn_ids)
        self.assertEqual([], delivery.calls)

    async def test_same_user_requests_are_not_cancelled_and_both_deliver(self) -> None:
        memory = FakeMemoryService()
        delivery = FakeDeliveryService()
        ollama = ConcurrentStreamOllama()
        service = ChatOnlyService(
            ollama=ollama,
            memory_service=memory,
            delivery_service=delivery,
            settings=Settings(),
        )

        first, second = await asyncio.gather(
            service.process_chat_request_result(ChatRequest(text="Перший", user_id="same-user"), "request-a"),
            service.process_chat_request_result(ChatRequest(text="Другий", user_id="same-user"), "request-b"),
        )

        self.assertEqual(2, ollama.started)
        self.assertEqual({"Відповідь request-a", "Відповідь request-b"}, {first.answer, second.answer})
        self.assertEqual(2, len(delivery.calls))
        self.assertTrue(first.delivered)
        self.assertTrue(second.delivered)

    async def test_stream_protocol_error_keeps_existing_fallback_behavior(self) -> None:
        memory = FakeMemoryService()
        delivery = FakeDeliveryService()
        service = ChatOnlyService(
            ollama=ProtocolErrorStreamOllama(),
            memory_service=memory,
            delivery_service=delivery,
            settings=Settings(),
        )

        result = await service.process_chat_request_result(
            ChatRequest(text="Тест помилки", user_id="test-user"),
            "protocol-error",
        )

        self.assertEqual("error", result.execution_outcome)
        self.assertEqual("stored", result.persistence_outcome)
        self.assertEqual("delivered", result.delivery_outcome)
        self.assertEqual("Не вдалося сформувати відповідь. Спробуй, будь ласка, ще раз.", result.answer)


if __name__ == "__main__":
    unittest.main()

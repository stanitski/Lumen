from __future__ import annotations

import unittest
import json
from unittest.mock import patch

from lumen.config import Settings
from lumen.services.memory import MemoryService


class FakeOllama:
    def __init__(self, content: str | None = None) -> None:
        self.content = content

    async def chat(self, **kwargs):
        self.kwargs = kwargs
        return {
            "message": {
                "content": self.content or (
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
        ollama = FakeOllama()
        service = MemoryService(object(), ollama, Settings())

        contract = await service.get_contract("Find information")

        self.assertEqual(
            ["telegram", "web", "home_assistant", "knowledge"],
            [item["source"] for item in contract["context_sources"]],
        )
        self.assertEqual("search", contract["context_sources"][0]["operation"])
        self.assertEqual("user", contract["context_sources"][0]["source_kind"])
        self.assertEqual("2026-07-15", contract["context_sources"][0]["date_from"])
        prompt = ollama.kwargs["messages"][0]["content"]
        self.assertIn('"reminder":{"action"', prompt)
        self.assertIn('"calendar":{"action"', prompt)
        self.assertIn("do not paraphrase, generalize, replace people, or invent details", prompt)
        self.assertIn("calendar creation command has priority", prompt)
        self.assertNotIn('"entity_id"', prompt)
        self.assertEqual({"action": "none", "item": {}}, contract["calendar"])

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

    def test_calendar_contract_normalizes_timed_event_and_discards_extra_fields(self) -> None:
        normalized = MemoryService._normalize_calendar_contract({
            "action": "create",
            "item": {
                "summary": "  Project meeting  ",
                "description": "  Discuss release  ",
                "location": "  Office  ",
                "all_day": False,
                "start": "2026-07-22T15:00:00",
                "end": "",
                "entity_id": "calendar.must_not_pass",
            },
            "unexpected": "discard",
        })

        self.assertEqual("create", normalized["action"])
        self.assertEqual({
            "summary": "Project meeting",
            "description": "Discuss release",
            "location": "Office",
            "all_day": False,
            "start": "2026-07-22T15:00:00+03:00",
            "end": "",
        }, normalized["item"])

    def test_calendar_contract_normalizes_all_day_with_exclusive_end(self) -> None:
        normalized = MemoryService._normalize_calendar_contract({
            "action": "create",
            "item": {
                "summary": "Conference",
                "description": "",
                "location": "Kyiv",
                "all_day": True,
                "start": "2026-07-22",
                "end": "2026-07-24",
            },
        })

        self.assertEqual("create", normalized["action"])
        self.assertEqual("2026-07-22", normalized["item"]["start"])
        self.assertEqual("2026-07-24", normalized["item"]["end"])

        default_end = MemoryService._normalize_calendar_contract({
            "action": "create",
            "item": {
                "summary": "One-day event",
                "description": "",
                "location": "",
                "all_day": True,
                "start": "2026-07-22",
                "end": "",
            },
        })
        self.assertEqual("create", default_end["action"])
        self.assertEqual("", default_end["item"]["end"])

    def test_calendar_contract_rejects_malformed_or_incomplete_create(self) -> None:
        invalid_contracts = (
            {"action": "create", "item": {"summary": "", "all_day": False, "start": "2026-07-22T15:00:00", "end": ""}},
            {"action": "create", "item": {"summary": "Meeting", "all_day": False, "start": "bad", "end": ""}},
            {"action": "create", "item": {"summary": "Meeting", "all_day": False, "start": "2026-07-22", "end": ""}},
            {"action": "create", "item": {"summary": "Meeting", "all_day": False, "start": "2026-07-22T16:00:00+03:00", "end": "2026-07-22T15:00:00+03:00"}},
            {"action": "create", "item": {"summary": "Holiday", "all_day": True, "start": "2026-07-22", "end": "2026-07-22"}},
            {"action": "create", "item": {"summary": "Meeting", "all_day": "false", "start": "2026-07-22T15:00:00", "end": ""}},
            {"action": "delete", "item": {}},
        )

        for raw in invalid_contracts:
            with self.subTest(raw=raw):
                self.assertEqual({"action": "none", "item": {}}, MemoryService._normalize_calendar_contract(raw))

    async def test_explicit_calendar_create_is_model_driven(self) -> None:
        ollama = FakeOllama(json.dumps({
            "topic": "calendar_event_creation",
            "answer_mode": "create_or_update",
            "time_scope": "explicit_period",
            "completeness": "normal",
            "context_sources": [],
            "calendar": {
                "action": "create",
                "item": {
                    "summary": "Project meeting",
                    "description": "Discuss release",
                    "location": "Office",
                    "all_day": False,
                    "start": "2026-07-22T15:00:00+03:00",
                    "end": "",
                },
            },
        }))
        service = MemoryService(object(), ollama, Settings())

        contract = await service.get_contract("Add a project meeting to my calendar")

        self.assertEqual("create", contract["calendar"]["action"])
        self.assertEqual("Project meeting", contract["calendar"]["item"]["summary"])
        prompt = ollama.kwargs["messages"][0]["content"]
        self.assertIn("only for an explicit command", prompt)
        self.assertIn("Do not check availability or conflicts", prompt)

    def test_reminder_contract_is_strictly_normalized(self) -> None:
        valid = MemoryService._normalize_reminder_contract({
            "action": "create",
            "items": [{"text": "пити воду", "date": "2026-07-21", "time": "09:00"}],
            "confirmation": "",
        })
        malformed = MemoryService._normalize_reminder_contract({"action": "cancel_list_item", "delete_ordinal": "2"})
        self.assertEqual("create", valid["action"])
        self.assertEqual("none", malformed["action"])

    def test_reminder_management_contract_normalizes_selector_and_partial_patch(self) -> None:
        normalized = MemoryService._normalize_reminder_contract({
            "action": "update",
            "selector": {"query": "  Пити\t таблетки ", "scope": "one"},
            "patch": {"text": "  Пити таблетки після сніданку ", "repeat_until": None},
            "confirmation": "yes",
        })

        self.assertEqual("update", normalized["action"])
        self.assertEqual({"query": "пити таблетки", "scope": "one"}, normalized["selector"])
        self.assertEqual(
            {"text": "Пити таблетки після сніданку", "repeat_until": None},
            normalized["patch"],
        )
        self.assertEqual([], normalized["items"])
        self.assertEqual("", normalized["confirmation"])

        selected = MemoryService._normalize_reminder_contract({
            "action": "select",
            "selector": {"session_token": "a" * 32, "ordinal": 2},
        })
        confirmed = MemoryService._normalize_reminder_contract({
            "action": "confirm_bulk",
            "selector": {"session_token": "b" * 32},
        })
        snoozed = MemoryService._normalize_reminder_contract({
            "action": "snooze",
            "selector": {"id": 7, "scope": "one"},
            "patch": {"snooze_minutes": 15},
        })
        bulk_deleted = MemoryService._normalize_reminder_contract({
            "action": "delete",
            "selector": {"scope": "all"},
        })

        self.assertEqual({"session_token": "a" * 32, "ordinal": 2}, selected["selector"])
        self.assertEqual({"session_token": "b" * 32}, confirmed["selector"])
        self.assertEqual({"snooze_minutes": 15}, snoozed["patch"])
        self.assertEqual({"scope": "all"}, bulk_deleted["selector"])

    def test_reminder_management_contract_rejects_malformed_selector_patch_and_tokens(self) -> None:
        malformed = (
            {"action": "update", "selector": {"id": True}, "patch": {"text": "текст"}},
            {"action": "update", "selector": {"id": 1, "query": "текст"}, "patch": {"text": "текст"}},
            {"action": "update", "selector": {"id": 1}, "patch": {"recurrence_weekday": 7}},
            {"action": "update", "selector": {"id": 1}, "patch": {"repeat_until": "not-a-date"}},
            {"action": "snooze", "selector": {"id": 1}, "patch": {"snooze_minutes": 0}},
            {"action": "select", "selector": {"session_token": "A" * 32, "ordinal": 1}},
            {"action": "select", "selector": {"session_token": "a" * 32, "ordinal": "1"}},
            {"action": "confirm_bulk", "selector": {"session_token": "a" * 32, "scope": "all"}},
            {"action": "pause", "selector": {"scope": "all"}},
        )

        for raw in malformed:
            with self.subTest(raw=raw):
                self.assertEqual("none", MemoryService._normalize_reminder_contract(raw)["action"])

    async def test_multi_time_reminder_discards_spurious_confirmation(self) -> None:
        ollama = FakeOllama(json.dumps({
            "topic": "medication_reminder",
            "answer_mode": "direct_answer",
            "time_scope": "none",
            "completeness": "normal",
            "context_sources": [],
            "reminder": {
                "action": "create",
                "items": [
                    {"text": "take medicine", "date": "2026-07-20", "time": "10:30", "recurrence_kind": "daily", "recurrence_weekday": None, "repeat_until": ""},
                    {"text": "take medicine", "date": "2026-07-20", "time": "23:30", "recurrence_kind": "daily", "recurrence_weekday": None, "repeat_until": ""},
                ],
                "delete_ordinal": None,
                "confirmation": "yes",
            },
        }))
        service = MemoryService(object(), ollama, Settings())

        contract = await service.get_contract("Щодня в 10:30 і в 23:30 нагадуй мені пити таблетки")

        reminder = contract["reminder"]
        self.assertEqual("create", reminder["action"])
        self.assertEqual(2, len(reminder["items"]))
        self.assertEqual("", reminder["confirmation"])
        self.assertIn("one create item per time", ollama.kwargs["messages"][0]["content"])

    async def test_reminder_prompt_uses_kyiv_date_without_system_local_time(self) -> None:
        ollama = FakeOllama()
        service = MemoryService(object(), ollama, Settings())

        with patch("lumen.services.memory.local_now", side_effect=AssertionError("system local time must not be used")):
            await service.get_contract("Нагадай завтра пити таблетки")

        prompt = ollama.kwargs["messages"][0]["content"]
        self.assertIn('"action":"none|create|list|update|pause|resume|delete|snooze|select|confirm_bulk"', prompt)
        self.assertIn("Use Europe/Kyiv for every reminder date and time.", prompt)
        self.assertIn("Today in the Europe/Kyiv timezone is", prompt)
        self.assertNotIn("cancel_list_item", prompt)

    def test_reminder_list_and_cancel_discard_spurious_confirmation(self) -> None:
        listed = MemoryService._normalize_reminder_contract({"action": "list", "confirmation": "yes"})
        cancelled = MemoryService._normalize_reminder_contract({
            "action": "cancel_list_item",
            "delete_ordinal": 3,
            "confirmation": "no",
        })

        self.assertEqual("list", listed["action"])
        self.assertEqual("", listed["confirmation"])
        self.assertEqual("cancel_list_item", cancelled["action"])
        self.assertEqual(3, cancelled["delete_ordinal"])
        self.assertEqual("", cancelled["confirmation"])


if __name__ == "__main__":
    unittest.main()

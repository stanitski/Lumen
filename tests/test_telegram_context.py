from __future__ import annotations

import unittest
import json

from lumen.config import Settings
from lumen.services.telegram_context import TelegramContextService


class TelegramContextPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        class FakeStore:
            def list_facts(self, user_id, limit, include_global):
                del user_id, limit, include_global
                return [
                    {"category": "messenger_context", "predicate": "uses", "tags": ["search_source", "telegram"], "value": json.dumps({"ref": "@allowedchat", "kind": "user"})},
                    {"category": "messenger_context", "predicate": "uses", "tags": ["search_source", "telegram"], "value": json.dumps({"ref": "https://t.me/allowedchannel", "kind": "channel"})},
                    {"category": "preference", "predicate": "uses", "tags": ["telegram", "search_source"], "value": json.dumps({"ref": "@blocked", "kind": "user"})},
                ]

        self.store = FakeStore()
        self.service = TelegramContextService(
            Settings(),
            self.store,
            speech_to_text=object(),
        )

    def test_selects_only_news_channels(self) -> None:
        selected = self.service._select_sources(
            request={"query": "news", "source_kind": "channel"}, user_text="What is in the news?", allowed_sources=self.service._allowed_sources("VST")
        )
        self.assertEqual(["https://t.me/allowedchannel"], [source.ref for source in selected])

    def test_selects_only_private_chat_sources(self) -> None:
        selected = self.service._select_sources(
            request={"query": "messages", "source_kind": "user"}, user_text="What did they write?", allowed_sources=self.service._allowed_sources("VST")
        )
        self.assertEqual(["@allowedchat"], [source.ref for source in selected])

    def test_unspecified_source_kind_does_not_read_all_sources(self) -> None:
        selected = self.service._select_sources(
            request={"query": "messages", "source_kind": "unspecified"}, user_text="Read Telegram", allowed_sources=self.service._allowed_sources("VST")
        )
        self.assertEqual([], selected)

    def test_rejects_explicit_source_outside_allowlist(self) -> None:
        selected = self.service._select_sources(request={"query": "@privatechat", "source_kind": "user"}, user_text="Read @privatechat", allowed_sources=self.service._allowed_sources("VST"))
        self.assertIsNone(selected)

    def test_accepts_explicit_allowed_source(self) -> None:
        selected = self.service._select_sources(request={"query": "@allowedchat", "source_kind": "user"}, user_text="Read @allowedchat", allowed_sources=self.service._allowed_sources("VST"))
        self.assertEqual(["@allowedchat"], [source.ref for source in selected])

    def test_reads_an_explicit_calendar_day_outside_default_window(self) -> None:
        start, end = self.service._requested_dates({"date_from": "2026-07-15", "date_to": "2026-07-15"})
        self.assertEqual("2026-07-15", start.isoformat())
        self.assertEqual("2026-07-15", end.isoformat())
        self.assertEqual((None, None), self.service._requested_dates({"date_from": "2026-07-16", "date_to": "2026-07-15"}))


if __name__ == "__main__":
    unittest.main()

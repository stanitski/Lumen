from __future__ import annotations

import asyncio
import unittest
import json
from unittest.mock import AsyncMock, Mock, patch

from telethon.tl.types import User, Chat

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

    def test_accepts_explicit_person_outside_allowlist(self) -> None:
        selected = self.service._select_sources(request={"query": "@privatechat", "source_kind": "user"}, user_text="Read @privatechat", allowed_sources=self.service._allowed_sources("VST"))
        self.assertEqual(["@privatechat"], [source.ref for source in selected])
        self.assertEqual("user", selected[0].kind)

    def test_explicit_person_works_without_catalog(self) -> None:
        selected = self.service._select_sources(request={"source_kind": "unspecified"}, user_text="Read @privatechat", allowed_sources=[])
        self.assertEqual(["@privatechat"], [source.ref for source in selected])

    def test_model_query_cannot_grant_person_access(self) -> None:
        selected = self.service._select_sources(request={"query": "@privatechat", "source_kind": "user"}, user_text="Read my messages", allowed_sources=[])
        self.assertIsNone(selected)

    def test_explicit_channel_and_group_still_require_allowlist(self) -> None:
        for kind in ("channel", "group"):
            with self.subTest(kind=kind):
                selected = self.service._select_sources(request={"source_kind": kind}, user_text="Read @privatechat", allowed_sources=[])
                self.assertIsNone(selected)

    def test_accepts_explicit_allowed_source(self) -> None:
        selected = self.service._select_sources(request={"query": "@allowedchat", "source_kind": "user"}, user_text="Read @allowedchat", allowed_sources=self.service._allowed_sources("VST"))
        self.assertEqual(["@allowedchat"], [source.ref for source in selected])

    def test_reads_an_explicit_calendar_day_outside_default_window(self) -> None:
        start, end = self.service._requested_dates({"date_from": "2026-07-15", "date_to": "2026-07-15"})
        self.assertEqual("2026-07-15", start.isoformat())
        self.assertEqual("2026-07-15", end.isoformat())
        self.assertEqual((None, None), self.service._requested_dates({"date_from": "2026-07-16", "date_to": "2026-07-15"}))

    def test_explicit_person_search_checks_entity_before_reading(self) -> None:
        self.service._settings.telegram_api_id = 1
        self.service._settings.telegram_api_hash = "test"
        self.service._settings.telegram_session_path = "test-session"
        for entity, should_read in ((User(id=123), True), (Chat(id=123, title="Group", photo=None, participants_count=1, date=None, version=1), False)):
            with self.subTest(should_read=should_read):
                client = AsyncMock()
                client.is_user_authorized.return_value = True
                client.get_entity.return_value = entity

                async def messages(*args, **kwargs):
                    yield type("Message", (), {"message": "hello", "date": None, "out": False})()

                client.iter_messages = Mock(side_effect=messages)
                with patch.object(self.service, "_allowed_sources", return_value=[]), patch("telethon.TelegramClient", return_value=client):
                    result = asyncio.run(self.service.search({"source_kind": "user"}, user_id="VST", user_text="Read @privatechat"))
                self.assertEqual("available" if should_read else "unavailable", result.status)
                self.assertEqual(1 if should_read else 0, client.iter_messages.call_count)
                client.disconnect.assert_awaited_once()

    def test_invalid_media_does_not_abort_telegram_lookup(self) -> None:
        class BrokenSpeechToText:
            async def transcribe_bytes(self, **kwargs):
                del kwargs
                raise IndexError("tuple index out of range")

        class FakeClient:
            async def download_media(self, message, *, file):
                del message, file
                return b"invalid-media"

        class FakeMessage:
            id = 42
            message = "Підпис до відео"
            video = True
            video_note = voice = audio = document = None

        self.service._speech_to_text = BrokenSpeechToText()

        text, media_kind = asyncio.run(self.service._message_text(FakeClient(), FakeMessage()))

        self.assertEqual("Підпис до відео", text)
        self.assertEqual("video", media_kind)

    def test_channel_media_uses_caption_without_download_or_transcription(self) -> None:
        class UnexpectedSpeechToText:
            async def transcribe_bytes(self, **kwargs):
                raise AssertionError(f"unexpected transcription: {kwargs}")

        class UnexpectedClient:
            async def download_media(self, message, *, file):
                raise AssertionError(f"unexpected download: {message}, {file}")

        class FakeMessage:
            message = "Текстовий підпис каналу"
            video = True
            video_note = voice = audio = document = None

        self.service._speech_to_text = UnexpectedSpeechToText()

        text, media_kind = asyncio.run(
            self.service._message_text(UnexpectedClient(), FakeMessage(), transcribe_media=False)
        )

        self.assertEqual("Текстовий підпис каналу", text)
        self.assertEqual("video", media_kind)


if __name__ == "__main__":
    unittest.main()

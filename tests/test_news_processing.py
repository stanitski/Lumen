from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import dataclass

from lumen.models import NewsProcessingRequest
from lumen.services.news_processing import NewsProcessingBusyError, NewsProcessingService


@dataclass
class Extracted:
    canonical_url: str
    text: str


class FakeExtractor:
    def __init__(self, result: object = None) -> None:
        self.result = result
        self.urls: list[str] = []

    async def extract(self, url: str) -> object:
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeOllama:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
        self.calls.append({"messages": messages, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return {"message": {"content": response}}
        return {"message": {"content": json.dumps(response, ensure_ascii=False)}}


class FakeMemory:
    def __init__(self, facts: list[dict[str, object]] | None = None) -> None:
        self.facts = facts or []

    def list_active_news_dislikes(self, *, user_id: str) -> list[dict[str, object]]:
        del user_id
        return self.facts


def classifier(*, decision: str = "keep", matched: list[str] | None = None) -> dict[str, object]:
    return {
        "editorial_decision": decision,
        "detected_topics": ["economy"],
        "matched_filter_subjects": matched or [],
        "reason_code": "advertisement" if decision == "skip" else None,
        "reason": "Реклама" if decision == "skip" else None,
    }


def five_w() -> dict[str, object]:
    return {
        "what": "Компанія повідомила про запуск.",
        "who": "Компанія",
        "where": "У Києві",
        "when": "Сьогодні",
        "why_or_context": "Це частина нового плану.",
        "narration": "Компанія повідомила про запуск нового сервісу. Подія сталася сьогодні у Києві.",
        "missing_fields": [],
    }


def request(**changes: object) -> NewsProcessingRequest:
    values: dict[str, object] = {
        "user_id": "VST",
        "feed_url": "https://agency.example/rss",
        "title": "Новина дня",
        "link": "https://agency.example/article?source=rss#part",
        "description": "RSS опис " * 30,
        "published_at": "2026-08-04T10:00:00Z",
    }
    values.update(changes)
    return NewsProcessingRequest(**values)


class NewsProcessingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_keep_uses_final_canonical_url_and_stable_todo_key(self) -> None:
        final_url = "https://agency.example/final-story"
        service = NewsProcessingService(
            extractor=FakeExtractor(Extracted(final_url, "Повний текст " * 40)),
            ollama=FakeOllama([classifier(), five_w()]),
            memory_service=FakeMemory(),
        )

        result = await service.process(request())

        self.assertEqual("keep", result.status)
        self.assertEqual(final_url, result.canonical_url)
        self.assertEqual(f"news:{result.news_id}|{final_url}", result.todo_item.item)
        self.assertEqual(2, len(service._ollama.calls))

    async def test_editorial_skip_does_not_call_editor(self) -> None:
        ollama = FakeOllama([classifier(decision="skip")])
        service = NewsProcessingService(
            extractor=FakeExtractor(Extracted("https://agency.example/a", "Текст " * 40)),
            ollama=ollama,
        )

        result = await service.process(request())

        self.assertEqual("skip", result.status)
        self.assertEqual("advertisement", result.reason_code)
        self.assertEqual(1, len(ollama.calls))

    async def test_only_disliked_topic_blocks_item(self) -> None:
        ollama = FakeOllama([classifier(matched=["topic:astrology"])])
        service = NewsProcessingService(
            extractor=FakeExtractor(Extracted("https://agency.example/a", "Текст " * 40)),
            ollama=ollama,
            memory_service=FakeMemory(
                [{"category": "news_context", "subject": "topic:astrology", "predicate": "dislikes", "value": "гороскопи"}]
            ),
        )

        result = await service.process(request())

        self.assertEqual("skip", result.status)
        self.assertEqual("user_disliked_topic", result.reason_code)
        self.assertEqual(1, len(ollama.calls))

    async def test_rss_fallback_requires_context_and_is_marked(self) -> None:
        service = NewsProcessingService(
            extractor=FakeExtractor(None),
            ollama=FakeOllama([classifier(), five_w()]),
        )

        result = await service.process(request(content="", description="RSS контекст " * 20))

        self.assertEqual("keep", result.status)
        self.assertEqual("rss", result.content_source)

    async def test_rss_fallback_uses_title_with_short_feedreader_description(self) -> None:
        description = "У призначенні є питання, що стосуються публічної репутації посадовця та майбутньої роботи служби."
        self.assertLess(len(description), NewsProcessingService.RSS_FALLBACK_MIN_CHARS)
        service = NewsProcessingService(
            extractor=FakeExtractor(None),
            ollama=FakeOllama([classifier(), five_w()]),
        )

        result = await service.process(
            request(
                title="Умєров очолив Службу зовнішньої розвідки: чому призначення викликало питання",
                content="",
                description=description,
            )
        )

        self.assertEqual("keep", result.status)
        self.assertEqual("rss", result.content_source)

    async def test_insufficient_feedreader_context_is_terminal_skip(self) -> None:
        ollama = FakeOllama([])
        service = NewsProcessingService(extractor=FakeExtractor(None), ollama=ollama)

        result = await service.process(request(content="", description="Короткий RSS опис."))

        self.assertEqual("skip", result.status)
        self.assertEqual("insufficient_source_context", result.reason_code)
        self.assertEqual("https://agency.example/article?source=rss", result.canonical_url)
        self.assertEqual([], ollama.calls)

    async def test_blank_five_w_fields_are_reported_deterministically(self) -> None:
        edited = five_w()
        edited["where"] = ""
        edited["missing_fields"] = []
        service = NewsProcessingService(
            extractor=FakeExtractor(Extracted("https://agency.example/a", "Текст " * 40)),
            ollama=FakeOllama([classifier(), edited]),
        )

        result = await service.process(request())

        self.assertEqual(["where"], result.missing_5w)

    async def test_malformed_editor_is_repaired_once(self) -> None:
        ollama = FakeOllama([classifier(), "not-json", five_w()])
        service = NewsProcessingService(
            extractor=FakeExtractor(Extracted("https://agency.example/a", "Текст " * 40)), ollama=ollama
        )

        result = await service.process(request())

        self.assertEqual("keep", result.status)
        self.assertEqual(3, len(ollama.calls))

    async def test_non_ukrainian_narration_is_repaired_once(self) -> None:
        invalid = five_w()
        invalid["narration"] = "The company launched a service. The event happened today."
        ollama = FakeOllama([classifier(), invalid, five_w()])
        service = NewsProcessingService(
            extractor=FakeExtractor(Extracted("https://agency.example/a", "Текст " * 40)), ollama=ollama
        )

        result = await service.process(request())

        self.assertEqual("keep", result.status)
        self.assertEqual(3, len(ollama.calls))

    async def test_invalid_input_is_terminal_skip(self) -> None:
        service = NewsProcessingService(extractor=FakeExtractor(), ollama=FakeOllama([]))

        result = await service.process(request(title="", link="not-a-url"))

        self.assertEqual("skip", result.status)
        self.assertEqual("invalid_input", result.reason_code)

    async def test_nonstandard_port_is_invalid_even_with_rss_fallback(self) -> None:
        service = NewsProcessingService(extractor=FakeExtractor(), ollama=FakeOllama([]))

        result = await service.process(request(link="https://agency.example:8123/article"))

        self.assertEqual("skip", result.status)
        self.assertEqual("invalid_input", result.reason_code)

    async def test_busy_request_raises_typed_error(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingExtractor:
            async def extract(self, url: str) -> Extracted:
                del url
                started.set()
                await release.wait()
                return Extracted("https://agency.example/a", "Текст " * 40)

        service = NewsProcessingService(extractor=BlockingExtractor(), ollama=FakeOllama([classifier(), five_w()]))
        first = asyncio.create_task(service.process(request()))
        await started.wait()
        with self.assertRaises(NewsProcessingBusyError) as raised:
            await service.process(request())
        self.assertEqual(60, raised.exception.retry_after)
        release.set()
        await first

    async def test_deadline_returns_retry(self) -> None:
        class SlowExtractor:
            async def extract(self, url: str) -> Extracted:
                del url
                await asyncio.sleep(0.1)
                return Extracted("https://agency.example/a", "Текст " * 40)

        service = NewsProcessingService(
            extractor=SlowExtractor(), ollama=FakeOllama([]), request_deadline_seconds=0.001
        )

        result = await service.process(request())

        self.assertEqual("retry", result.status)
        self.assertEqual("processing_timeout", result.reason_code)

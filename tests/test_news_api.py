from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.runtime_test_support import main
from lumen.models import NewsProcessingResult
from lumen.services.news_processing import NewsProcessingBusyError


class FakeNewsProcessingService:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.calls: list[tuple[object, str]] = []

    async def process(self, payload: object, user_id: str) -> NewsProcessingResult:
        self.calls.append((payload, user_id))
        if self.busy:
            raise NewsProcessingBusyError()
        return NewsProcessingResult(
            status="keep",
            news_id="abc",
            content_source="rss",
            canonical_url="https://agency.example/article",
        )


class NewsProcessingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = main.news_processing_service
        self.fake = FakeNewsProcessingService()
        main.news_processing_service = self.fake
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.news_processing_service = self.original

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "user_id": "news-user",
            "feed_url": "https://agency.example/rss",
            "title": "Заголовок",
            "link": "https://agency.example/article",
            "description": "Опис",
        }

    def test_endpoint_processes_inline_with_request_user(self) -> None:
        response = self.client.post("/news/process", json=self._payload())

        self.assertEqual(200, response.status_code)
        self.assertEqual("keep", response.json()["status"])
        self.assertEqual("news-user", self.fake.calls[0][1])

    def test_endpoint_falls_back_to_configured_user(self) -> None:
        payload = self._payload()
        payload.pop("user_id")

        response = self.client.post("/news/process", json=payload)

        self.assertEqual(200, response.status_code)
        self.assertEqual(main._user_id(None), self.fake.calls[0][1])

    def test_busy_endpoint_returns_429_and_retry_after(self) -> None:
        self.fake.busy = True

        response = self.client.post("/news/process", json=self._payload())

        self.assertEqual(429, response.status_code)
        self.assertEqual("60", response.headers["retry-after"])


if __name__ == "__main__":
    unittest.main()

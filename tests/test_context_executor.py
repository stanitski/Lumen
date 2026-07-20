from __future__ import annotations

import unittest

from lumen.services.context_executor import ContextExecutor
from lumen.services.web_search import WebSearchResult


class FakeWebSearch:
    def __init__(self, results: list[WebSearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    async def search(self, query: str) -> list[WebSearchResult]:
        self.queries.append(query)
        return self.results


class ContextExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_sources_remain_truthful(self) -> None:
        executor = ContextExecutor()

        results = await executor.execute(
            [
                {"source": "telegram", "operation": "search", "query": "Jan", "scope": "approved_chats"},
                {"source": "web", "operation": "search", "query": "weather", "scope": "default"},
            ],
            user_id="VST",
            user_text="Find messages",
        )

        self.assertEqual(["telegram", "web"], [result.source for result in results])
        self.assertEqual(["pending_implementation", "unavailable"], [result.status for result in results])
        self.assertIn("Do not claim", executor.format_for_prompt(results))

    async def test_unknown_or_invalid_source_is_ignored(self) -> None:
        results = await ContextExecutor().execute(
            [{"source": "unknown"}, {"source": "telegram", "operation": "send"}], user_id="VST", user_text="test"
        )
        self.assertEqual([], results)

    async def test_web_search_uses_contract_query_and_bounds_context(self) -> None:
        search = FakeWebSearch(
            [
                WebSearchResult(f"Result {index}", f"https://example{index}.test", "snippet " * 100, "page " * 400)
                for index in range(5)
            ]
        )
        executor = ContextExecutor(web_search=search)

        results = await executor.execute(
            [{"source": "web", "operation": "search", "query": "exact user query", "scope": "default"}],
            user_id="VST",
            user_text="different original message",
        )

        self.assertEqual(["exact user query"], search.queries)
        self.assertEqual("available", results[0].status)
        self.assertLessEqual(len(results[0].context), executor._WEB_CONTEXT_CHAR_LIMIT)
        self.assertIn("https://example0.test", results[0].context)

    async def test_web_search_failure_is_truthfully_unavailable(self) -> None:
        executor = ContextExecutor(web_search=FakeWebSearch([]))

        results = await executor.execute(
            [{"source": "web", "operation": "search", "query": "weather", "scope": "default"}],
            user_id="VST",
            user_text="weather",
        )

        self.assertEqual("unavailable", results[0].status)
        self.assertIn("Do not claim", executor.format_for_prompt(results))


if __name__ == "__main__":
    unittest.main()

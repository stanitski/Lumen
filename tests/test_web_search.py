import unittest
from unittest.mock import patch

import httpx

from lumen.services.web_search import (
    DuckDuckGoWebSearch,
    ExtractedWebPage,
    WebPageExtractor,
    _extract_main_text,
    _unwrap_duckduckgo_url,
)


async def _public_resolver(hostname: str, port: int) -> list[tuple[object, ...]]:
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class WebPageExtractorTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_checked_redirect_and_returns_canonical_page(self) -> None:
        requests: list[str] = []
        resolved_hosts: list[str] = []

        async def recording_resolver(hostname: str, port: int) -> list[tuple[object, ...]]:
            resolved_hosts.append(hostname)
            return await _public_resolver(hostname, port)

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.host == "agency.example":
                return httpx.Response(302, headers={"location": "https://article.example/final?fbclid=tracker"})
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<p>article</p>")

        extractor = WebPageExtractor(transport=httpx.MockTransport(handler), resolver=recording_resolver)
        with patch("lumen.services.web_search._extract_main_text", return_value="The extracted article text"):
            result = await extractor.extract("https://agency.example/news?topic=one&utm_source=rss#section", char_limit=14)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.canonical_url, "https://article.example/final")
        self.assertEqual(result.text, "The extracted")
        self.assertEqual(requests, ["https://agency.example/news?topic=one", "https://article.example/final"])
        self.assertEqual(resolved_hosts, ["agency.example", "article.example"])

    async def test_rejects_non_global_resolution_before_request(self) -> None:
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>never read</p>")

        async def private_resolver(hostname: str, port: int) -> list[tuple[object, ...]]:
            return [(2, 1, 6, "", ("10.0.0.5", port))]

        extractor = WebPageExtractor(transport=httpx.MockTransport(handler), resolver=private_resolver)
        self.assertIsNone(await extractor.extract("https://internal.example/news"))
        self.assertEqual(request_count, 0)

    async def test_rejects_compressed_or_oversized_or_non_html_response(self) -> None:
        body_limit = WebPageExtractor.MAX_BODY_BYTES

        for path, headers, body in (
            ("compressed", {"content-type": "text/html", "content-encoding": "gzip"}, b"compressed"),
            ("large", {"content-type": "text/html"}, b"x" * (body_limit + 1)),
            ("json", {"content-type": "application/json"}, b"{}"),
        ):
            with self.subTest(path=path):
                def handler(request: httpx.Request, *, headers: dict[str, str] = headers, body: bytes = body) -> httpx.Response:
                    return httpx.Response(200, headers=headers, content=body)

                extractor = WebPageExtractor(transport=httpx.MockTransport(handler), resolver=_public_resolver)
                self.assertIsNone(await extractor.extract(f"https://example.com/{path}"))

    def test_extracts_main_text_with_trafilatura(self) -> None:
        html = """
        <html><body><nav>Navigation</nav><article><h1>Important report</h1>
        <p>The first paragraph contains the confirmed event and its context.</p>
        <p>The second paragraph adds the responsible people and location.</p>
        <p>The third paragraph explains why the event matters to readers.</p>
        </article><footer>Footer links</footer></body></html>
        """

        text = _extract_main_text(html)

        self.assertIn("Important report", text)
        self.assertIn("confirmed event", text)


class _FakeExtractor:
    def __init__(self, search_html: str) -> None:
        self.search_html = search_html
        self.fetch_urls: list[str] = []
        self.extract_calls: list[tuple[str, int]] = []

    async def fetch_html(self, url: str):
        self.fetch_urls.append(url)
        return type("SearchPage", (), {"html": self.search_html})()

    async def extract(self, url: str, *, char_limit: int):
        self.extract_calls.append((url, char_limit))
        return ExtractedWebPage(url, "<p>page</p>", f"text from {url}")


class DuckDuckGoWebSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_unwraps_duckduckgo_result_url(self) -> None:
        wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews%3Fid%3D7"

        self.assertEqual("https://example.com/news?id=7", _unwrap_duckduckgo_url(wrapped))

    async def test_keeps_five_results_and_enriches_only_first_two(self) -> None:
        results_html = "".join(
            f'<a class="result__a" href="https://site{number}.example/news?utm_source=ddg&id={number}">Title {number}</a>'
            f'<div class="result__snippet">Snippet {number}</div>'
            for number in range(6)
        )
        extractor = _FakeExtractor(results_html)

        results = await DuckDuckGoWebSearch(page_extractor=extractor).search("news query")

        self.assertEqual(len(results), 5)
        self.assertEqual(len(extractor.fetch_urls), 1)
        self.assertEqual(extractor.extract_calls, [
            ("https://site0.example/news?id=0", 1_200),
            ("https://site1.example/news?id=1", 1_200),
        ])
        self.assertTrue(results[0].page_text)
        self.assertEqual(results[2].page_text, "")


if __name__ == "__main__":
    unittest.main()

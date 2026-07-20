"""Bounded DuckDuckGo search adapter for read-only web context."""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    page_text: str = ""


class _DuckDuckGoResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._active: str | None = None
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._active = "title"
            self._href = attributes.get("href") or ""
            self._parts = []
        elif "result__snippet" in classes:
            self._active = "snippet"
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._active:
            return
        value = _normalize_text(" ".join(self._parts))
        if self._active == "title" and tag == "a":
            self.results.append({"title": value, "url": self._href, "snippet": ""})
            self._active = None
        elif self._active == "snippet" and tag in {"a", "div", "span"}:
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = value
            self._active = None


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


class DuckDuckGoWebSearch:
    """Fetch a fixed, bounded DuckDuckGo result set without query rewriting."""

    RESULT_LIMIT = 5
    PAGE_LIMIT = 2
    PAGE_TEXT_CHAR_LIMIT = 1200

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("lumen")

    async def search(self, query: str) -> list[WebSearchResult]:
        query = _clip_text(query, 500)
        if not query:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=True,
                headers={"User-Agent": "Lumen/1.0 (local assistant)"},
            ) as client:
                response = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
                response.raise_for_status()
                parsed = self._parse_results(response.text)
                results = [WebSearchResult(**item) for item in parsed[:self.RESULT_LIMIT]]
                enriched: list[WebSearchResult] = []
                for index, result in enumerate(results):
                    page_text = await self._fetch_page_text(client, result.url) if index < self.PAGE_LIMIT else ""
                    enriched.append(WebSearchResult(result.title, result.url, result.snippet, page_text))
        except httpx.HTTPError as exc:
            self._logger.warning("DuckDuckGo web search unavailable query_chars=%d error=%s", len(query), type(exc).__name__)
            return []

        self._logger.debug(
            "DuckDuckGo web search query_chars=%d results=%d pages_read=%d",
            len(query), len(enriched), min(len(enriched), self.PAGE_LIMIT),
        )
        return enriched

    @staticmethod
    def _parse_results(html: str) -> list[dict[str, str]]:
        parser = _DuckDuckGoResultsParser()
        parser.feed(html)
        results: list[dict[str, str]] = []
        for item in parser.results:
            url = _unwrap_duckduckgo_url(item["url"])
            if item["title"] and _is_public_http_url(url):
                results.append({"title": item["title"], "url": url, "snippet": item["snippet"]})
        return results

    async def _fetch_page_text(self, client: httpx.AsyncClient, url: str) -> str:
        if not _is_public_http_url(url):
            return ""
        try:
            response = await client.get(url)
            response.raise_for_status()
            if not _is_public_http_url(str(response.url)):
                return ""
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.casefold():
                return ""
            parser = _PageTextParser()
            parser.feed(response.text)
            return _clip_text(_normalize_text(" ".join(parser.parts)), self.PAGE_TEXT_CHAR_LIMIT)
        except (httpx.HTTPError, UnicodeError):
            return ""


def _unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        return unquote(parse_qs(parsed.query).get("uddg", [""])[0])
    return url


def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed.hostname.casefold() not in {"localhost", "localhost.localdomain"}
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clip_text(value: str, limit: int) -> str:
    value = _normalize_text(value)
    if len(value) <= limit:
        return value
    boundary = value.rfind(" ", 0, limit)
    return value[:boundary if boundary > 0 else limit].rstrip()

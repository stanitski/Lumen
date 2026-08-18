"""Bounded public-web retrieval for read-only context."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx


_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_TRACKING_QUERY_KEYS = {"dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid", "_ga", "_gl"}


@dataclass(frozen=True)
class ExtractedWebPage:
    canonical_url: str
    html: str
    text: str


@dataclass(frozen=True)
class _FetchedHtml:
    canonical_url: str
    html: str


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


class WebPageExtractor:
    """Safely fetch and extract a public HTML page with bounded resource use."""

    DEFAULT_TEXT_CHAR_LIMIT = 12_000
    MAX_REDIRECTS = 5
    MAX_BODY_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str, int], Awaitable[list[tuple[Any, ...]]]] | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("lumen")
        self._transport = transport
        self._resolver = resolver or self._resolve_addresses

    async def extract(self, url: str, *, char_limit: int = DEFAULT_TEXT_CHAR_LIMIT) -> ExtractedWebPage | None:
        """Return canonical URL and Trafilatura main text, or ``None`` if unsuitable."""
        if char_limit <= 0:
            return None
        page = await self.fetch_html(url)
        if page is None:
            return None
        text = await asyncio.to_thread(_extract_main_text, page.html)
        return ExtractedWebPage(page.canonical_url, page.html, _clip_text(text, char_limit))

    async def fetch_html(self, url: str) -> _FetchedHtml | None:
        """Fetch one public HTML page, checking every redirect target."""
        current_url = _canonicalize_url(url)
        if current_url is None:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                trust_env=False,
                cookies={},
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity",
                    "User-Agent": "Lumen/1.0 (local assistant)",
                },
                transport=self._transport,
            ) as client:
                for redirect_count in range(self.MAX_REDIRECTS + 1):
                    if not await self._host_resolves_publicly(current_url):
                        return None
                    response = await self._get_response(client, current_url)
                    if response is None:
                        return None
                    status_code, headers, body = response
                    if status_code in _REDIRECT_STATUS_CODES:
                        location = headers.get("location")
                        if not location or redirect_count == self.MAX_REDIRECTS:
                            return None
                        redirected_url = _canonicalize_url(urljoin(current_url, location))
                        if redirected_url is None:
                            return None
                        current_url = redirected_url
                        continue
                    if status_code != 200 or not _is_html_content_type(headers.get("content-type", "")):
                        return None
                    return _FetchedHtml(_canonicalize_url(current_url) or current_url, _decode_html(body, headers))
        except (httpx.HTTPError, UnicodeError, ValueError) as exc:
            self._logger.debug("Public web page unavailable error=%s", type(exc).__name__)
        return None

    async def _host_resolves_publicly(self, url: str) -> bool:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        try:
            addresses = await self._resolver(hostname, parsed.port or _default_port(parsed.scheme))
        except (OSError, ValueError):
            return False
        return bool(addresses) and all(_is_global_address(item[4][0]) for item in addresses)

    @staticmethod
    async def _resolve_addresses(hostname: str, port: int) -> list[tuple[Any, ...]]:
        return await asyncio.to_thread(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM)

    async def _get_response(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[int, httpx.Headers, bytes] | None:
        async with client.stream("GET", url) as response:
            content_encoding = response.headers.get("content-encoding", "").casefold().strip()
            if content_encoding and content_encoding != "identity":
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.MAX_BODY_BYTES:
                    return None
            return response.status_code, response.headers, bytes(body)


class DuckDuckGoWebSearch:
    """Fetch a fixed, bounded DuckDuckGo result set without query rewriting."""

    RESULT_LIMIT = 5
    PAGE_LIMIT = 2
    PAGE_TEXT_CHAR_LIMIT = 1_200

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        page_extractor: WebPageExtractor | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("lumen")
        self._page_extractor = page_extractor or WebPageExtractor(logger=self._logger)

    async def search(self, query: str) -> list[WebSearchResult]:
        query = _clip_text(query, 500)
        if not query:
            return []
        search_url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
        search_page = await self._page_extractor.fetch_html(search_url)
        if search_page is None:
            self._logger.warning("DuckDuckGo web search unavailable query_chars=%d", len(query))
            return []
        parsed = self._parse_results(search_page.html)
        results = [WebSearchResult(**item) for item in parsed[:self.RESULT_LIMIT]]
        enriched: list[WebSearchResult] = []
        for index, result in enumerate(results):
            page = (
                await self._page_extractor.extract(result.url, char_limit=self.PAGE_TEXT_CHAR_LIMIT)
                if index < self.PAGE_LIMIT
                else None
            )
            enriched.append(
                WebSearchResult(
                    result.title,
                    page.canonical_url if page else result.url,
                    result.snippet,
                    page.text if page else "",
                )
            )
        self._logger.debug(
            "DuckDuckGo web search query_chars=%d results=%d pages_read=%d",
            len(query),
            len(enriched),
            min(len(enriched), self.PAGE_LIMIT),
        )
        return enriched

    @staticmethod
    def _parse_results(html: str) -> list[dict[str, str]]:
        parser = _DuckDuckGoResultsParser()
        parser.feed(html)
        results: list[dict[str, str]] = []
        for item in parser.results:
            url = _unwrap_duckduckgo_url(item["url"])
            canonical_url = _canonicalize_url(url)
            if item["title"] and canonical_url:
                results.append({"title": item["title"], "url": canonical_url, "snippet": item["snippet"]})
        return results


def _unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        return unquote(parse_qs(parsed.query).get("uddg", [""])[0])
    return url


def _canonicalize_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            return None
    except ValueError:
        return None
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _is_tracking_query_key(key)
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))


def _is_tracking_query_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized.startswith("utm_") or normalized in _TRACKING_QUERY_KEYS


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _is_global_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _is_html_content_type(value: str) -> bool:
    return value.split(";", 1)[0].casefold().strip() in _HTML_CONTENT_TYPES


def _decode_html(body: bytes, headers: httpx.Headers) -> str:
    charset = "utf-8"
    content_type = headers.get("content-type", "")
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    if charset_match:
        charset = charset_match.group(1).strip('"\'')
    return body.decode(charset)


def _extract_main_text(html: str) -> str:
    try:
        import trafilatura
    except ImportError:
        return ""
    return _normalize_text(trafilatura.extract(html, include_comments=False, include_tables=False) or "")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clip_text(value: str, limit: int) -> str:
    value = _normalize_text(value)
    if len(value) <= limit:
        return value
    boundary = value.rfind(" ", 0, limit)
    return value[:boundary if boundary > 0 else limit].rstrip()

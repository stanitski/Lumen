"""Bounded processing of raw Feedreader items into spoken news Todo entries."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urldefrag, urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from lumen.models import (
    NewsFiveW,
    NewsProcessingRequest,
    NewsProcessingResult,
    NewsTodoItem,
)


class NewsProcessingBusyError(RuntimeError):
    """A caller may turn this error into HTTP 429 with ``retry_after``."""

    retry_after = 60

    def __init__(self) -> None:
        super().__init__("News processing is already in progress.")


class ArticleExtractor(Protocol):
    async def extract(self, url: str) -> object:
        """Return article text and its final canonical URL, or raise on failure."""


class StructuredOllama(Protocol):
    async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
        """Perform one non-streaming structured Ollama chat call."""


class _NewsClassification(BaseModel):
    editorial_decision: str
    detected_topics: list[str] = Field(default_factory=list)
    matched_filter_subjects: list[str] = Field(default_factory=list)
    reason_code: str | None = None
    reason: str | None = None

    @field_validator("editorial_decision")
    @classmethod
    def _only_editorial_outcomes(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"keep", "skip"}:
            raise ValueError("editorial_decision must be keep or skip")
        return value


class _FiveWPayload(BaseModel):
    what: str = ""
    who: str = ""
    where: str = ""
    when: str = ""
    why_or_context: str = ""
    narration: str
    missing_fields: list[str] = Field(default_factory=list)

    @field_validator("what", "who", "where", "when", "why_or_context", mode="before")
    @classmethod
    def _normalize_five_w_field(cls, value: object) -> str:
        return _normalize_text(str(value or ""))

    @model_validator(mode="after")
    def _complete_missing_fields(self) -> "_FiveWPayload":
        field_names = ("what", "who", "where", "when", "why_or_context")
        missing = {str(item).strip() for item in self.missing_fields if str(item).strip() in field_names}
        missing.update(name for name in field_names if not getattr(self, name))
        self.missing_fields = [name for name in field_names if name in missing]
        return self

    @field_validator("narration")
    @classmethod
    def _validate_narration(cls, value: str) -> str:
        narration = _normalize_text(value)
        if not narration:
            raise ValueError("narration is required")
        if re.search(r"https?://|www\.|[`*_#\[\]<>]", narration, flags=re.IGNORECASE):
            raise ValueError("narration must not contain Markdown or URLs")
        sentence_count = len(re.findall(r"[.!?](?=\s|$)", narration))
        if not 2 <= sentence_count <= 4:
            raise ValueError("narration must contain 2 to 4 sentences")
        letters = re.findall(r"[^\W\d_]", narration, flags=re.UNICODE)
        cyrillic_letters = re.findall(r"[\u0400-\u04ff]", narration)
        if not letters or len(cyrillic_letters) / len(letters) < 0.7:
            raise ValueError("narration must be written in Ukrainian")
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", narration):
            raise ValueError("narration contains unsupported foreign-script fragments")
        lowered = narration.casefold()
        if any(
            phrase in lowered
            for phrase in (
                "ось короткий виклад",
                "у цій новині розповідається",
                "новина розповідає",
                "текст повідомляє",
                "як штучний інтелект",
            )
        ):
            raise ValueError("narration must not contain meta-commentary")
        return narration


class NewsProcessingService:
    """A serial, deadline-bound news pipeline with dependency-light adapters.

    The service purposely accepts structural protocols instead of concrete services: the
    extractor and precise news-memory query are supplied by the API wiring layer.
    """

    REQUEST_DEADLINE_SECONDS = 180.0
    RSS_FALLBACK_MIN_CHARS = 160
    ARTICLE_CONTEXT_CHAR_LIMIT = 12_000

    def __init__(
        self,
        *,
        extractor: ArticleExtractor,
        ollama: StructuredOllama,
        memory_service: object | None = None,
        memory_store: object | None = None,
        default_user_id: str = "global",
        request_deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._extractor = extractor
        self._ollama = ollama
        self._memory_service = memory_service
        self._memory_store = memory_store
        self._default_user_id = default_user_id.strip() or "global"
        self._request_deadline_seconds = request_deadline_seconds
        self._logger = logger or logging.getLogger("lumen")
        self._in_flight = asyncio.Lock()

    async def process(
        self,
        request: NewsProcessingRequest | Mapping[str, object],
        user_id: str | None = None,
    ) -> NewsProcessingResult:
        """Process one item, returning retry for transient work failures.

        ``NewsProcessingBusyError`` is deliberately not converted to ``retry``: an
        HTTP ingress must expose it as 429 so Feedreader can obey the retry interval.
        """

        if not self._in_flight.locked():
            await self._in_flight.acquire()
        else:
            raise NewsProcessingBusyError()

        try:
            try:
                payload = request if isinstance(request, NewsProcessingRequest) else NewsProcessingRequest.model_validate(request)
            except ValidationError:
                return self._skip_invalid(None)
            resolved_user_id = (user_id or payload.user_id or self._default_user_id).strip() or self._default_user_id
            try:
                return await asyncio.wait_for(
                    self._process(payload, resolved_user_id), timeout=self._request_deadline_seconds
                )
            except TimeoutError:
                return self._retry(payload, "processing_timeout", "Вичерпано час обробки новини.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Network/model integration failures are retriable.
                self._logger.warning("News processing failed error=%s", type(exc).__name__)
                return self._retry(payload, "processing_failed", "Тимчасова помилка обробки новини.")
        finally:
            self._in_flight.release()

    async def _process(self, request: NewsProcessingRequest, user_id: str) -> NewsProcessingResult:
        title = _normalize_text(request.title)
        initial_url = _canonical_http_url(request.link)
        if not title or not initial_url:
            return self._skip_invalid(request)

        article = await self._extract_article(initial_url)
        rss_context = _rss_context(request)
        if article is not None:
            context, canonical_url = article
            content_source = "article"
        elif len(rss_context) >= self.RSS_FALLBACK_MIN_CHARS:
            context, canonical_url, content_source = rss_context, initial_url, "rss"
        else:
            return self._retry(request, "content_unavailable", "Не вдалося отримати придатний текст новини.")

        news_id = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        known_dislikes = await self._active_dislikes(user_id)
        classification = await self._classify(
            title=title,
            url=canonical_url,
            context=context,
            known_filter_subjects=sorted(known_dislikes),
        )
        if classification.editorial_decision == "skip":
            return NewsProcessingResult(
                status="skip",
                news_id=news_id,
                content_source=content_source,
                canonical_url=canonical_url,
                published_at=request.published_at,
                reason_code=classification.reason_code or "editorial_rejection",
                reason=classification.reason or "Редакційний фільтр відхилив матеріал.",
            )

        matched_subjects = {
            _normalize_subject(item)
            for item in classification.matched_filter_subjects
            if _normalize_subject(item) in known_dislikes
        }
        if matched_subjects:
            labels = [known_dislikes[subject] for subject in sorted(matched_subjects)]
            return NewsProcessingResult(
                status="skip",
                news_id=news_id,
                content_source=content_source,
                canonical_url=canonical_url,
                published_at=request.published_at,
                reason_code="user_disliked_topic",
                reason=f"Користувач відмовився від теми: {', '.join(labels)}.",
            )

        five_w = await self._edit_five_w(title=title, url=canonical_url, context=context)
        return NewsProcessingResult(
            status="keep",
            news_id=news_id,
            content_source=content_source,
            canonical_url=canonical_url,
            published_at=request.published_at,
            todo_item=NewsTodoItem(item=f"news:{news_id}|{canonical_url}", description=five_w.narration),
            missing_5w=five_w.missing_fields,
        )

    async def _extract_article(self, url: str) -> tuple[str, str] | None:
        try:
            extracted = await self._extractor.extract(url)
        except Exception as exc:
            self._logger.info("News article extraction unavailable error=%s", type(exc).__name__)
            return None
        text, canonical_url = _extraction_parts(extracted, url)
        if not text:
            return None
        return text[: self.ARTICLE_CONTEXT_CHAR_LIMIT], canonical_url

    async def _active_dislikes(self, user_id: str) -> dict[str, str]:
        """Read only exact active news dislikes; never use news likes as a gate."""

        for dependency in (self._memory_service, self._memory_store):
            if dependency is None:
                continue
            method = getattr(dependency, "list_active_news_dislikes", None)
            if callable(method):
                facts = await _maybe_await(method(user_id=user_id))
                return _dislike_subjects(facts)

        # Compatibility for current stores until their narrow query is wired.
        if self._memory_store is not None:
            method = getattr(self._memory_store, "list_facts", None)
            if callable(method):
                facts = await _maybe_await(method(user_id, include_global=False))
                return _dislike_subjects(facts)
        return {}

    async def _classify(
        self,
        *,
        title: str,
        url: str,
        context: str,
        known_filter_subjects: list[str],
    ) -> _NewsClassification:
        messages = [
            {
                "role": "system",
                "content": (
                    "Classify a candidate news item. Return JSON only. editorial_decision may be keep or skip. "
                    "Skip only advertisements, duplicates, navigation fragments, empty/unusable material, or other "
                    "editorial/technical junk. Never skip a valid news item because of its subject, political position, "
                    "country, or a topic preference. detected_topics is descriptive. matched_filter_subjects may contain "
                    "only exact subjects from Known filter subjects."
                ),
            },
            {"role": "system", "content": f"Known filter subjects: {json.dumps(known_filter_subjects, ensure_ascii=False)}"},
            {"role": "user", "content": f"Title: {title}\nURL: {url}\nSource text:\n{context}"},
        ]
        return await self._structured_call(_NewsClassification, messages)

    async def _edit_five_w(self, *, title: str, url: str, context: str) -> NewsFiveW:
        messages = self._five_w_messages(title=title, url=url, context=context)
        try:
            payload = await self._structured_call(_FiveWPayload, messages)
        except (ValidationError, ValueError) as exc:
            repair_messages = [
                *messages,
                {
                    "role": "system",
                    "content": (
                        "Your previous response did not satisfy the required JSON structure. Repair it now. "
                        f"Validation error: {str(exc)[:300]}"
                    ),
                },
            ]
            payload = await self._structured_call(_FiveWPayload, repair_messages)
        return NewsFiveW.model_validate(payload.model_dump())

    @staticmethod
    def _five_w_messages(*, title: str, url: str, context: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Produce a Ukrainian 5W news brief as JSON only: what, who, where, when, why_or_context, narration, "
                    "missing_fields. Use only facts in Source text; do not infer, embellish, or claim verification beyond it. "
                    "Use an empty string and list the field in missing_fields when unavailable. narration must be 2 to 4 "
                    "plain Ukrainian sentences, with no Markdown, URLs, meta-commentary, or headings."
                ),
            },
            {"role": "user", "content": f"Title: {title}\nURL: {url}\nSource text:\n{context}"},
        ]

    async def _structured_call(self, schema: type[BaseModel], messages: list[dict[str, str]]) -> Any:
        response = await self._ollama.chat(
            messages=messages,
            response_format=schema.model_json_schema(),
            options={"temperature": 0},
            think=False,
            include_runtime_context=False,
        )
        raw = _ollama_content(response)
        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Malformed structured Ollama response: {exc}") from exc

    @staticmethod
    def _skip_invalid(request: NewsProcessingRequest | None) -> NewsProcessingResult:
        return NewsProcessingResult(
            status="skip",
            published_at=request.published_at if request else None,
            reason_code="invalid_input",
            reason="Відсутній або некоректний заголовок чи URL новини.",
        )

    @staticmethod
    def _retry(request: NewsProcessingRequest, reason_code: str, reason: str) -> NewsProcessingResult:
        return NewsProcessingResult(
            status="retry",
            canonical_url=_canonical_http_url(request.link) or "",
            published_at=request.published_at,
            reason_code=reason_code,
            reason=reason,
        )


async def _maybe_await(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


def _extraction_parts(extracted: object, fallback_url: str) -> tuple[str, str]:
    if isinstance(extracted, str):
        return _normalize_text(extracted), fallback_url
    if isinstance(extracted, Mapping):
        text = extracted.get("text", extracted.get("content", extracted.get("page_text", "")))
        url = extracted.get("canonical_url", extracted.get("url", fallback_url))
    else:
        text = getattr(extracted, "text", getattr(extracted, "content", getattr(extracted, "page_text", "")))
        url = getattr(extracted, "canonical_url", getattr(extracted, "url", fallback_url))
    return _normalize_text(str(text or "")), _canonical_http_url(str(url or "")) or fallback_url


def _ollama_content(response: object) -> object:
    if isinstance(response, BaseModel):
        return response.model_dump()
    if not isinstance(response, Mapping):
        raise ValueError("Ollama response is not an object")
    message = response.get("message")
    content = message.get("content") if isinstance(message, Mapping) else response.get("content", response)
    if isinstance(content, Mapping):
        return content
    if not isinstance(content, str):
        raise ValueError("Ollama response does not contain JSON content")
    try:
        parsed = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("Ollama response contains invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Ollama response JSON must be an object")
    return parsed


def _dislike_subjects(facts: object) -> dict[str, str]:
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
        return {}
    result: dict[str, str] = {}
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        if str(fact.get("category", "")).casefold() != "news_context":
            continue
        if str(fact.get("predicate", "")).casefold() != "dislikes":
            continue
        subject = _normalize_subject(str(fact.get("subject", "")))
        if subject:
            result[subject] = _normalize_text(str(fact.get("value", ""))) or subject.removeprefix("topic:")
    return result


def _normalize_subject(value: str) -> str:
    normalized = _normalize_text(value).casefold()
    return normalized if normalized.startswith("topic:") and len(normalized) > len("topic:") else ""


def _rss_context(request: NewsProcessingRequest) -> str:
    """Return all editorial text that Feedreader supplied for a fallback brief."""

    return _normalize_text("\n".join(item for item in (request.title, request.content, request.description) if item))


def _canonical_http_url(value: str) -> str | None:
    candidate = value.strip()
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return None
    return urldefrag(candidate)[0]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

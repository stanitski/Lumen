"""Local operator inspection and editing of the existing memory runtime."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lumen.memory.fact_policy import fact_revision, is_mandatory_fact, is_prompt_fact


def require_local_admin(request: Request, response: Response) -> None:
    """Keep the operator interface local and block browser cross-origin writes."""
    host = request.url.hostname or ""
    peer = request.client.host if request.client else ""
    try:
        local_peer = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local_peer = False
    if not local_peer or host not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "Адміністрування пам’яті доступне лише з цього комп’ютера.")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    origin = request.headers.get("origin")
    expected = urlsplit(str(request.url))
    if origin:
        try:
            supplied = urlsplit(origin)
        except ValueError as exc:
            raise HTTPException(403, "Некоректне джерело запиту.") from exc
        if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
            raise HTTPException(403, "Запит має походити зі сторінки адміністрування Lumen.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Міжсайтові запити до пам’яті заборонено.")


class FactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revision: str = Field(min_length=64, max_length=64)
    category: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=120)
    predicate: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=10000)
    inclusion: Literal["mandatory", "contextual"]

    @field_validator("category")
    @classmethod
    def public_category(cls, value: str) -> str:
        if value.casefold() in {"secret", "credential"}:
            raise ValueError("Secret categories cannot be edited in the memory dashboard")
        return value


class FactDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)


class MemoryPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=10000)
    generate_answer: bool = True


class MemoryAdminService:
    def __init__(self, store, *, fact_context, chat_service, user_id: str, logger) -> None:
        self._store = store
        self._fact_context = fact_context
        self._chat_service = chat_service
        self._user_id = user_id
        self._logger = logger

    @staticmethod
    def _public(fact: dict) -> bool:
        return str(fact.get("category", "")).strip().casefold() not in {"secret", "credential"}

    def _view(self, fact: dict) -> dict:
        return {
            **fact,
            "revision": fact_revision(fact),
            "inclusion": "mandatory" if is_mandatory_fact(fact) else "contextual",
            "editable": fact.get("user_id") == self._user_id,
        }

    def _owned_fact(self, fact_id: int) -> dict:
        fact = self._store.get_fact(self._user_id, fact_id)
        if fact is None or not self._public(fact):
            raise HTTPException(404, "Факт не знайдено або він недоступний для редагування.")
        return fact

    def items(self) -> dict:
        facts = []
        for scope in dict.fromkeys((self._user_id, "global")):
            after_id = 0
            while True:
                page = self._store.list_fact_page(scope, after_id=after_id)
                if not page:
                    break
                facts.extend(self._view(fact) for fact in page if self._public(fact))
                after_id = max(int(fact["id"]) for fact in page)
        counts = self._store.memory_counts(self._user_id)
        counts["facts"] = len(facts)
        # The inventory stays lightweight; full historic excerpts come from retrieval.
        return {"user_id": self._user_id, "counts": counts, "facts": facts,
                "turns": [], "knowledge": []}

    def fact_details(self, fact_id: int) -> dict:
        fact = self._store.get_fact(self._user_id, fact_id)
        if fact is None:
            fact = self._store.get_fact("global", fact_id)
        if fact is None or not self._public(fact):
            raise HTTPException(404, "Факт не знайдено.")
        source_turn = None
        match = re.fullmatch(r"(?:dialogue:)?turn:(\d+)", str(fact.get("source_ref", "")))
        if match:
            turn = self._store.get_turn(user_id=fact["user_id"], turn_id=int(match.group(1)))
            if turn:
                source_turn = {key: getattr(turn, key) for key in
                               ("id", "question", "answer", "created_at", "source")}
        return {"fact": self._view(fact), "source_turn": source_turn}

    async def update_fact(self, fact_id: int, payload: FactEdit) -> dict:
        original = await asyncio.to_thread(self._owned_fact, fact_id)
        if payload.inclusion == "mandatory" and (
            self._user_id == "global" or payload.category.casefold() in {"device_alias", "messenger_context"}
        ):
            raise HTTPException(422, "Ця категорія не може бути постійним правилом спілкування.")
        expires_at = original.get("expires_at")
        try:
            fact = await asyncio.to_thread(
                self._store.update_fact, fact_id, user_id=self._user_id,
                category=payload.category, subject=payload.subject, predicate=payload.predicate,
                value=payload.value, confidence=original["confidence"], importance=original["importance"],
                source_ref=original["source_ref"], tags=original.get("tags", []),
                expires_at=datetime.fromisoformat(expires_at.replace("Z", "+00:00")) if expires_at else None,
                inclusion=payload.inclusion, expected_revision=payload.revision,
            )
        except ValueError as exc:
            if str(exc) != "Fact changed since it was loaded":
                raise
            raise HTTPException(409, "Факт уже змінено. Перезавантаж його та перевір зміни перед збереженням.") from exc
        if fact is None:
            raise HTTPException(404, "Факт уже видалено.")
        index = {"status": "not_required"}
        if is_prompt_fact(fact, self._user_id) and not is_mandatory_fact(fact):
            try:
                result = await self._fact_context.sync_index(user_id=self._user_id)
                if result.get("failed", 0) or result.get("stale_race", 0):
                    index = {"status": "failed", "message": "Факт збережено, але пошуковий індекс не повністю оновлено. Тимчасово працює текстовий пошук."}
                else:
                    index = {"status": "indexed"}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("Admin fact index update failed: %s", type(exc).__name__)
                index = {"status": "failed", "message": "Факт збережено, але індексація недоступна. Тимчасово працює текстовий пошук."}
        return {"fact": self._view(fact), "index": index}

    def delete_fact(self, fact_id: int, revision: str) -> dict:
        self._owned_fact(fact_id)
        try:
            deleted = self._store.delete_fact(self._user_id, fact_id, expected_revision=revision)
        except ValueError as exc:
            if str(exc) != "Fact changed since it was loaded":
                raise
            raise HTTPException(409, "Факт уже змінено. Перезавантаж його перед видаленням.") from exc
        if not deleted:
            raise HTTPException(404, "Факт уже видалено.")
        return {"deleted": True}

    async def preview(self, text: str, generate_answer: bool) -> dict:
        messages, context = await self._chat_service._build_dialogue_messages(
            user_text=text, user_id=self._user_id)
        # Freeze retrieval before model generation. Never reload current records
        # to explain a past answer: a concurrent edit must produce a 409 later.
        context = copy.deepcopy(context)
        for key in ("selected_facts", "mandatory_facts", "contextual_facts"):
            context[key] = [self._view(fact) for fact in context.get(key, []) if self._public(fact)]
        context["prompt_messages"] = copy.deepcopy(messages)
        answer = None
        if generate_answer:
            try:
                payload = await self._chat_service._ollama.chat(
                    messages=messages, model=self._chat_service._settings.ollama_dialogue_model or None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("Admin preview generation failed: %s", type(exc).__name__)
                raise HTTPException(502, "Не вдалося отримати тестову відповідь Lumen. Спробуй ще раз.") from exc
            answer = str(payload.get("message", {}).get("content", "")).strip()
            if not answer:
                raise HTTPException(502, "Lumen повернув порожню тестову відповідь.")
        return jsonable_encoder({"answer": answer, "context": context,
                                 "message_count": len(messages),
                                 "created_at": datetime.now(timezone.utc).isoformat()})

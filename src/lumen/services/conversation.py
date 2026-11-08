"""Shared persistence and delivery lifecycle for assistant conversation events."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from lumen.models import ChatResult


class ConversationService:
    def __init__(self, memory_service, delivery_service, logger: logging.Logger | None = None,
                 task_starter: Callable[[Coroutine[Any, Any, None]], object] | None = None) -> None:
        self._memory = memory_service
        self._delivery = delivery_service
        self._logger = logger or logging.getLogger("lumen")
        self._task_starter = task_starter
        self._indexing: set[tuple[str, int]] = set()

    def get_event_result(self, *, user_id: str, event_id: str) -> ChatResult | None:
        lookup = getattr(self._memory, "get_conversation_event", None)
        if not callable(lookup):
            return None
        turn = lookup(user_id=user_id, event_id=event_id)
        if turn is None:
            return None
        meta = turn.turn_meta if isinstance(turn.turn_meta, dict) else {}
        delivered = bool(meta.get("callback_accepted"))
        delivery_status = str(meta.get("delivery_status", "not_attempted"))
        if delivery_status not in {"delivered", "domain_delivered", "inline", "suppressed", "failed", "not_attempted"}:
            delivery_status = "not_attempted"
        outcome = str(meta.get("outcome", "ok"))
        if outcome not in {"ok", "partial", "error", "awaiting_confirmation", "cancelled_idle"}:
            outcome = "ok"
        return ChatResult(
            request_id=str(meta.get("request_id", event_id)),
            user_id=user_id,
            answer=turn.answer,
            delivered=delivered,
            execution_outcome=outcome,
            persistence_outcome="stored",
            delivery_outcome=delivery_status,
            metadata={"turn_id": turn.id, **meta, "question": turn.question},
        )

    async def finish(
        self,
        *,
        user_id: str,
        question: str,
        answer: str,
        request_id: str,
        source: str = "chat",
        kind: str = "chat",
        event_id: str | None = None,
        callback_metadata: dict[str, object] | None = None,
        turn_meta: dict[str, object] | None = None,
        deliver: bool = True,
        voice: bool = False,
        execution_outcome: str = "ok",
        store_memory: bool = True,
    ) -> ChatResult:
        cached: ChatResult | None = None
        replacing_error = False
        turn_id: int | None = None
        if store_memory and event_id:
            cached = self.get_event_result(user_id=user_id, event_id=event_id)
            if cached is not None:
                if cached.execution_outcome == "error" and execution_outcome != "error":
                    replacing_error = True
                    raw_turn_id = cached.metadata.get("turn_id")
                    turn_id = raw_turn_id if isinstance(raw_turn_id, int) and not isinstance(raw_turn_id, bool) else None
                else:
                    if cached.delivered or not deliver:
                        return cached
                    # Retry the persisted response and its original service metadata.
                    answer = cached.answer
                    question = str(cached.metadata.get("question", question))
                    request_id = cached.request_id
                    saved_metadata = cached.metadata.get("callback_metadata")
                    if isinstance(saved_metadata, dict):
                        callback_metadata = {**(callback_metadata or {}), **saved_metadata}
        metadata = dict(turn_meta or {})
        metadata.update({"kind": kind, "outcome": execution_outcome, "request_id": request_id})
        if event_id:
            metadata["event_id"] = event_id
        if callback_metadata:
            metadata["callback_metadata"] = dict(callback_metadata)
            chat_id = callback_metadata.get("chat_id", callback_metadata.get("target_chat_id"))
            if chat_id is not None:
                metadata["chat_id"] = str(chat_id)
        if store_memory and replacing_error:
            updater = getattr(self._memory, "update_turn_result", None)
            if turn_id is None or not callable(updater):
                return ChatResult(
                    request_id=request_id, user_id=user_id, answer=answer, voice=voice,
                    execution_outcome=execution_outcome, persistence_outcome="failed",
                    delivery_outcome="not_attempted",
                    metadata={"persistence_error": "Unable to replace the cached error result."},
                )
            try:
                metadata["callback_accepted"] = False
                metadata["delivery_status"] = "not_attempted" if deliver else "inline"
                updated = updater(user_id=user_id, turn_id=turn_id, question=question, answer=answer,
                                  updates=metadata)
                if not updated:
                    raise RuntimeError("Cached conversation event disappeared before result update")
            except Exception as exc:
                self._logger.exception("Conversation result replacement failed request=%s: %s", request_id, exc)
                return ChatResult(
                    request_id=request_id, user_id=user_id, answer=answer, voice=voice,
                    execution_outcome=execution_outcome, persistence_outcome="failed",
                    delivery_outcome="not_attempted",
                    metadata={"persistence_error": str(exc).strip() or exc.__class__.__name__},
                )
        elif store_memory:
            try:
                add_once = getattr(self._memory, "add_turn_once", None)
                if event_id and callable(add_once):
                    turn_id = add_once(
                        user_id=user_id, event_id=event_id, source=source,
                        question=question, answer=answer, turn_meta=metadata,
                    )
                else:
                    if event_id:
                        metadata["event_id"] = event_id
                    # Keep compatibility with existing test doubles and callers.
                    turn_id = self._memory.add_turn(
                        user_id=user_id, source=source, question=question,
                        answer=answer, turn_meta=metadata,
                    )
            except Exception as exc:
                self._logger.exception("Conversation persistence failed request=%s: %s", request_id, exc)
                return ChatResult(
                    request_id=request_id, user_id=user_id, answer=answer, voice=voice,
                    execution_outcome=execution_outcome, persistence_outcome="failed",
                    delivery_outcome="not_attempted",
                    metadata={"persistence_error": str(exc).strip() or exc.__class__.__name__},
                )

        delivered = False
        delivery_outcome = "inline" if not deliver else "not_attempted"
        callback_accepted = False
        if deliver:
            try:
                await self._delivery.deliver_chat_answer(
                    request_id=request_id, user_id=user_id, answer=answer, voice=voice,
                    metadata=callback_metadata,
                )
                delivered = True
                callback_accepted = True
                delivery_outcome = "delivered"
            except Exception as exc:
                delivery_outcome = "failed"
                metadata["delivery_error"] = str(exc).strip() or exc.__class__.__name__
                self._logger.exception("Conversation delivery failed request=%s: %s", request_id, exc)
        metadata["callback_accepted"] = callback_accepted
        metadata["delivery_status"] = delivery_outcome
        if store_memory and turn_id is not None:
            try:
                update = getattr(self._memory, "update_turn_metadata", None)
                if callable(update):
                    update(user_id=user_id, turn_id=turn_id, updates={
                        "callback_accepted": callback_accepted,
                        "delivery_status": delivery_outcome,
                        **({"delivery_error": metadata["delivery_error"]} if "delivery_error" in metadata else {}),
                    })
                stored = self._memory.get_conversation_event(user_id=user_id, event_id=event_id) if event_id and callable(getattr(self._memory, "get_conversation_event", None)) else None
                if stored is None or not stored.memory_processed:
                    await self._index_turn(user_id=user_id, turn_id=turn_id)
            except Exception as exc:
                metadata["post_processing_error"] = str(exc).strip() or exc.__class__.__name__
                self._logger.warning("Conversation post-processing failed turn=%s: %s", turn_id, exc)
        return ChatResult(
            request_id=request_id, user_id=user_id, answer=answer, voice=voice, delivered=delivered,
            execution_outcome=execution_outcome,
            persistence_outcome="stored" if store_memory else "skipped",
            delivery_outcome=delivery_outcome, metadata=metadata,
        )

    async def record_event(
        self,
        *,
        user_id: str,
        role: str = "assistant",
        content: str,
        question: str = "",
        source: str = "reminder",
        kind: str = "reminder_notification",
        event_id: str | None = None,
        metadata: dict[str, object] | None = None,
        execution_outcome: str = "ok",
    ) -> int:
        event_metadata = dict(metadata or {})
        if event_metadata.get("target_chat_id") is not None:
            event_metadata["chat_id"] = str(event_metadata["target_chat_id"])
        event_metadata.update({
            "kind": kind, "outcome": execution_outcome, "role": role,
            "delivery_status": "domain_delivered" if role == "assistant" else "suppressed",
            "callback_accepted": role == "assistant",
        })
        add_once = getattr(self._memory, "add_turn_once", None)
        if event_id and callable(add_once):
            turn_id = add_once(
                user_id=user_id, event_id=event_id, source=source,
                question=question, answer=content, turn_meta=event_metadata,
            )
        else:
            if event_id:
                event_metadata["event_id"] = event_id
            turn_id = self._memory.add_turn(
                user_id=user_id, source=source, question=question,
                answer=content, turn_meta=event_metadata,
            )
        stored = self._memory.get_conversation_event(user_id=user_id, event_id=event_id) if event_id and callable(getattr(self._memory, "get_conversation_event", None)) else None
        if stored is None or not stored.memory_processed:
            await self._index_turn(user_id=user_id, turn_id=turn_id)
        return turn_id

    async def _index_turn(self, *, user_id: str, turn_id: int) -> None:
        if self._task_starter is None:
            await self._memory.postprocess_dialogue_turn(user_id=user_id, turn_id=turn_id)
            return
        key = (user_id, turn_id)
        if key in self._indexing:
            return
        self._indexing.add(key)
        async def run() -> None:
            try:
                await self._memory.postprocess_dialogue_turn(user_id=user_id, turn_id=turn_id)
            finally:
                self._indexing.discard(key)
        coroutine = run()
        try:
            self._task_starter(coroutine)
        except Exception:
            coroutine.close()
            self._indexing.discard(key)
            raise

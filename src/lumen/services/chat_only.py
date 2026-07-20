"""Minimal dialogue runtime used while optional Lumen services are disabled."""

from __future__ import annotations

import logging

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.models import ChatRequest, ChatResult
from lumen.services.delivery import DeliveryService
from lumen.services.context_executor import ContextExecutor
from lumen.services.memory import MemoryService


class ChatOnlyService:
    """Handle dialogue, profile memory, and delivery without skills or execution."""

    def __init__(
        self,
        *,
        ollama: OllamaConnector,
        memory_service: MemoryService,
        delivery_service: DeliveryService,
        settings: Settings,
        context_executor: ContextExecutor | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._ollama = ollama
        self._memory_service = memory_service
        self._delivery_service = delivery_service
        self._settings = settings
        self._context_executor = context_executor
        self._logger = logger or logging.getLogger("lumen")

    async def process_chat_request(self, payload: ChatRequest, request_id: str) -> None:
        await self.process_chat_request_result(payload, request_id, deliver=True)

    async def process_chat_request_with_context(
        self,
        payload: ChatRequest,
        request_id: str,
        *,
        supplemental_context: str = "",
        callback_metadata: dict[str, object] | None = None,
        include_supplemental_context_in_planning: bool = False,
    ) -> None:
        # Attachments and planner context are disabled in chat_only mode.
        del include_supplemental_context_in_planning
        await self.process_chat_request_result(
            payload,
            request_id,
            supplemental_context=supplemental_context,
            callback_metadata=callback_metadata,
            deliver=True,
        )

    async def process_chat_request_result(
        self,
        payload: ChatRequest,
        request_id: str,
        *,
        supplemental_context: str = "",
        callback_metadata: dict[str, object] | None = None,
        deliver: bool = True,
        include_supplemental_context_in_planning: bool = False,
    ) -> ChatResult:
        del include_supplemental_context_in_planning
        user_id = self._memory_service.resolve_user_id(payload)
        store_memory = user_id.casefold() != "global" and not self._memory_disabled(payload.metadata, callback_metadata)
        voice = False
        answer = ""
        execution_outcome = "ok"
        try:
            answer = await self._generate_answer(
                user_text=payload.text,
                user_id=user_id,
                supplemental_context=supplemental_context,
            )
        except Exception as exc:
            execution_outcome = "error"
            answer = "Не вдалося сформувати відповідь. Спробуй, будь ласка, ще раз."
            self._logger.exception("Chat-only dialogue failed for request %s: %s", request_id, exc)

        persistence_outcome = "skipped"
        metadata = dict(callback_metadata or {})
        turn_id: int | None = None
        if store_memory:
            try:
                turn_id = self._memory_service.add_turn(
                    user_id=user_id,
                    source="learning-api",
                    question=payload.text,
                    answer=answer,
                    turn_meta={
                        "v": 1,
                        "outcome": execution_outcome,
                        "kind": "chat",
                    },
                )
                persistence_outcome = "stored"
                self._logger.info("Chat-only turn persisted request=%s turn_id=%s", request_id, turn_id)
            except Exception as exc:
                persistence_outcome = "failed"
                metadata["persistence_error"] = str(exc).strip() or exc.__class__.__name__
                self._logger.exception("Chat-only persistence failed for request %s: %s", request_id, exc)

        delivered = False
        delivery_outcome = "inline" if not deliver else "not_attempted"
        if deliver:
            try:
                await self._delivery_service.deliver_chat_answer(
                    request_id=request_id,
                    user_id=user_id,
                    answer=answer,
                    voice=voice,
                    metadata=callback_metadata,
                )
                delivered = True
                delivery_outcome = "delivered"
            except Exception as exc:
                delivery_outcome = "failed"
                metadata["delivery_error"] = str(exc).strip() or exc.__class__.__name__
                self._logger.exception("Chat-only delivery failed for request %s: %s", request_id, exc)

        if turn_id is not None:
            try:
                await self._memory_service.postprocess_dialogue_turn(user_id=user_id, turn_id=turn_id)
            except Exception as exc:
                metadata["post_processing_error"] = str(exc).strip() or exc.__class__.__name__
                self._logger.warning("Chat-only post-processing failed for turn %s: %s", turn_id, exc)

        return ChatResult(
            request_id=request_id,
            user_id=user_id,
            answer=answer,
            voice=voice,
            delivered=delivered,
            execution_outcome=execution_outcome,
            persistence_outcome=persistence_outcome,
            delivery_outcome=delivery_outcome,
            metadata=metadata,
        )

    async def _generate_answer(self, *, user_text: str, user_id: str, supplemental_context: str = "") -> str:
        messages, _ = await self._build_dialogue_messages(
            user_text=user_text,
            user_id=user_id,
            supplemental_context=supplemental_context,
        )
        payload = await self._ollama.chat(messages=messages, model=self._settings.ollama_dialogue_model or None)
        answer = str(payload.get("message", {}).get("content", "")).strip()
        return answer or "Не зміг сформувати відповідь. Спробуй, будь ласка, перефразувати запит."

    async def debug_memory_search(self, *, user_text: str, user_id: str) -> dict[str, object]:
        messages, context = await self._build_dialogue_messages(user_text=user_text, user_id=user_id)
        payload = await self._ollama.chat(messages=messages, model=self._settings.ollama_dialogue_model or None)
        return {
            "answer": str(payload.get("message", {}).get("content", "")).strip(),
            "context": context,
            "message_count": len(messages),
        }

    async def _build_dialogue_messages(
        self,
        *,
        user_text: str,
        user_id: str,
        supplemental_context: str = "",
    ) -> tuple[list[dict[str, str]], dict[str, object]]:
        context = await self._memory_service.build_planner_context(
            payload_text=user_text,
            user_id=user_id,
            include_knowledge=True,
            include_recent_turns=False,
            include_topic_expansion=False,
            force_semantic_dialogue=True,
        )
        retrieval_policy = context.get("retrieval_policy")
        include_previous_turn = not isinstance(retrieval_policy, dict) or bool(
            retrieval_policy.get("include_previous_turn", True)
        )
        previous_turns = (
            self._memory_service.recent_dialogue_turns(user_id=user_id, limit=1) if include_previous_turn else []
        )
        previous_turn_messages = (
            [
                {"role": "user", "content": previous_turns[0].question},
                {"role": "assistant", "content": previous_turns[0].answer},
            ]
            if previous_turns
            else []
        )
        context["previous_turns"] = previous_turns
        contract = context.get("query_contract")
        context_sources = contract.get("context_sources") if isinstance(contract, dict) else []
        context_results = (
            await self._context_executor.execute(context_sources, user_id=user_id, user_text=user_text)
            if self._context_executor is not None
            else []
        )
        context["context_execution"] = [result.as_dict() for result in context_results]
        contract_message = (
            [{"role": "system", "content": self._format_answer_contract(contract)}]
            if isinstance(contract, dict)
            else []
        )
        return (
            [
                {
                    "role": "system",
                    "content": (
                        "You are Lumen, a helpful personal assistant. Reply directly to the user in Ukrainian. "
                        "Use memory only when it is clearly relevant. Do not claim to perform actions, create reminders, "
                        "control devices, or access files. Use web facts only when supplied in external context and cite their URL. Do not output JSON. "
                        "The current user message has the highest priority; use the previous turn only for clear follow-ups."
                    ),
                },
                *contract_message,
                *(
                    [{"role": "system", "content": self._context_executor.format_for_prompt(context_results)}]
                    if context_results and self._context_executor is not None
                    else []
                ),
                *(
                    [{"role": "system", "content": supplemental_context.strip()}]
                    if supplemental_context.strip()
                    else []
                ),
                {"role": "system", "content": str(context["memory_context"])},
                {"role": "system", "content": str(context["dialogue_context"])},
                {"role": "system", "content": str(context["knowledge_context"])},
                *previous_turn_messages,
                {"role": "user", "content": user_text},
            ],
            context,
        )

    @staticmethod
    def _format_answer_contract(contract: dict[str, object]) -> str:
        answer_mode = str(contract.get("answer_mode", "direct_answer"))
        time_scope = str(contract.get("time_scope", "none"))
        completeness = str(contract.get("completeness", "normal"))
        instruction = "Use the supplied context only when it is relevant."
        if answer_mode == "list_records" and completeness == "complete":
            instruction = (
                "List every confirmed relevant record from the supplied dialogue context. "
                "Separate records from commentary and state conflicts instead of silently omitting details."
            )
        return (
            f"Answer contract: mode={answer_mode}; time_scope={time_scope}; completeness={completeness}. "
            f"{instruction}"
        )

    @staticmethod
    def _memory_disabled(*metadata_sets: dict[str, object] | None) -> bool:
        for metadata in metadata_sets:
            if not isinstance(metadata, dict):
                continue
            for key in ("debug", "debug_request", "lumen_debug", "skip_memory", "disable_memory"):
                value = metadata.get(key)
                if value is True or (isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}):
                    return True
        return False

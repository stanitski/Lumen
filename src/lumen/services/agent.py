from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from lumen.actions import ActionRegistry
from lumen.connectors.home_assistant import HomeAssistantConnector
from lumen.connectors.ollama import OllamaConnector
from lumen.knowledge.store import KnowledgeStore
from lumen.memory.store import MemoryStore
from lumen.models import ActionProposal, AgentRequest, AgentResponse, Citation, ConfirmActionRequest

logger = logging.getLogger(__name__)


@dataclass
class AgentService:
    memory_store: MemoryStore
    knowledge_store: KnowledgeStore
    ollama: OllamaConnector
    home_assistant: HomeAssistantConnector
    action_registry: ActionRegistry
    session_factory: Callable

    async def ask(self, request: AgentRequest) -> AgentResponse:
        self.memory_store.log_message(
            conversation_id=request.conversation_id,
            session_id=request.session_id,
            user_id=request.user_id,
            source=request.source,
            role="user",
            message=request.text,
        )

        recent_conversation = self.memory_store.recent_conversation(request.conversation_id, limit=8)
        memory_hits = self.memory_store.search(request.text, limit=5)
        knowledge_hits = self.knowledge_store.search(request.text, limit=5)
        entity_hits = await self._entity_context_hits(request)
        live_state_hits = await self._live_state_context_hits(request)
        deterministic_answer = await self._deterministic_home_assistant_answer(request)
        logger.info(
            "agent.ask context conversation_id=%s source=%s memory_hits=%d knowledge_hits=%d entity_hits=%d live_state_hits=%d recent_conversation=%d deterministic_answer=%s",
            request.conversation_id,
            request.source,
            len(memory_hits),
            len(knowledge_hits),
            len(entity_hits),
            len(live_state_hits),
            len(recent_conversation),
            bool(deterministic_answer),
        )
        model_answer = deterministic_answer or await self._answer_with_model(
            request,
            memory_hits,
            knowledge_hits,
            entity_hits,
            live_state_hits,
            recent_conversation,
        )

        self.memory_store.log_message(
            conversation_id=request.conversation_id,
            session_id=request.session_id,
            user_id=request.user_id,
            source=request.source,
            role="assistant",
            message=model_answer,
        )
        await self._extract_and_store_memory(request)

        action_proposal = await self._build_action_proposal(request, recent_conversation) if request.allow_actions else None
        citations = [
            Citation(
                source_type=item.source_type,
                source_ref=item.source_ref,
                title=item.title,
                snippet=item.snippet,
            )
            for item in knowledge_hits[:3]
        ]
        answer = model_answer
        if action_proposal is not None:
            answer = f"{model_answer}\n\nI can prepare this Home Assistant action for confirmation."
        return AgentResponse(
            answer=answer,
            citations=citations,
            memory_hits=memory_hits,
            knowledge_hits=knowledge_hits,
            action_proposal=action_proposal,
            requires_confirmation=action_proposal is not None,
        )

    async def confirm_action(self, request: ConfirmActionRequest) -> AgentResponse:
        with self.session_factory() as connection:
            trace = connection.execute(
                "SELECT * FROM action_traces WHERE action_id = ?",
                (request.action_id,),
            ).fetchone()
            if trace is None or trace["status"] != "pending":
                logger.warning(
                    "agent.confirm_action missing_or_not_pending action_id=%s conversation_id=%s",
                    request.action_id,
                    request.conversation_id,
                )
                return AgentResponse(answer="No pending action was found for that action_id.")

            if not request.confirmed:
                logger.info("agent.confirm_action cancelled action_id=%s", request.action_id)
                connection.execute(
                    "UPDATE action_traces SET status = ?, result_message = ?, confirmed_at = ? WHERE action_id = ?",
                    ("cancelled", "User declined the proposed action.", datetime.now(timezone.utc).isoformat(), request.action_id),
                )
                connection.commit()
                return AgentResponse(answer="Action cancelled. Nothing was executed.")

            service_data = json.loads(trace["service_data_json"])
            if not self.action_registry.is_allowed(trace["ha_domain"], trace["ha_service"], service_data):
                logger.warning(
                    "agent.confirm_action blocked action_id=%s domain=%s service=%s",
                    request.action_id,
                    trace["ha_domain"],
                    trace["ha_service"],
                )
                connection.execute(
                    "UPDATE action_traces SET status = ?, result_message = ? WHERE action_id = ?",
                    ("blocked", "Action is not on the allowlist.", request.action_id),
                )
                connection.commit()
                return AgentResponse(answer="Action blocked because it is not allowlisted.")

            if not self.home_assistant.available:
                logger.warning("agent.confirm_action home_assistant_unavailable action_id=%s", request.action_id)
                connection.execute(
                    "UPDATE action_traces SET status = ?, result_message = ?, confirmed_at = ? WHERE action_id = ?",
                    ("failed", "Home Assistant connector is not configured.", datetime.now(timezone.utc).isoformat(), request.action_id),
                )
                connection.commit()
                return AgentResponse(answer="Home Assistant is unavailable, so I could not execute the action.")

            result = await self.home_assistant.execute_service(trace["ha_domain"], trace["ha_service"], service_data)
            logger.info(
                "agent.confirm_action executed action_id=%s domain=%s service=%s result=%s",
                request.action_id,
                trace["ha_domain"],
                trace["ha_service"],
                result,
            )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                UPDATE action_traces
                SET status = ?, result_message = ?, confirmed_at = ?, executed_at = ?
                WHERE action_id = ?
                """,
                ("executed", f"Executed successfully: {result}", now, now, request.action_id),
            )
            connection.commit()
            return AgentResponse(answer=f"Action executed: {trace['label']}")

    async def _answer_with_model(self, request: AgentRequest, memory_hits, knowledge_hits, entity_hits, live_state_hits, recent_conversation) -> str:
        context_bits = []
        conversation_context = self._format_recent_conversation(recent_conversation, current_text=request.text)
        if conversation_context:
            context_bits.append("Recent conversation:\n" + conversation_context)
        if memory_hits:
            context_bits.append(
                "Memory:\n" + "\n".join(f"- {item.subject} {item.predicate} {item.value}" for item in memory_hits)
            )
        if knowledge_hits:
            context_bits.append(
                "Knowledge:\n" + "\n".join(f"- {item.title}: {item.snippet}" for item in knowledge_hits[:3])
            )
        if entity_hits:
            context_bits.append("Exposed entities:\n" + "\n".join(f"- {entity_id}" for entity_id in entity_hits))
        if live_state_hits:
            context_bits.append("Live Home Assistant state:\n" + "\n".join(f"- {item}" for item in live_state_hits))
        system_prompt = (
            "You are LUMEN, a cautious Home Assistant-first household AI assistant. "
            "Use the provided memory and knowledge context, do not claim to execute actions without confirmation, "
            "and answer concisely. Prefer simple Telegram-friendly markdown structure such as short paragraphs or flat lists."
        )
        messages = [{"role": "system", "content": system_prompt}]
        if context_bits:
            messages.append({"role": "system", "content": "\n\n".join(context_bits)})
        messages.append({"role": "user", "content": request.text})

        if not self.ollama.available:
            logger.warning("agent.ask ollama_unavailable conversation_id=%s", request.conversation_id)
            return self._fallback_answer(request, memory_hits, knowledge_hits)

        try:
            logger.info(
                "agent.ask ollama_request conversation_id=%s model=%s message_count=%d",
                request.conversation_id,
                getattr(self.ollama, "model", "<unknown>"),
                len(messages),
            )
            payload = await self.ollama.chat(messages)
            content = payload.get("message", {}).get("content", "").strip()
            if content:
                logger.info(
                    "agent.ask ollama_response conversation_id=%s has_content=%s content=%r",
                    request.conversation_id,
                    bool(content),
                    content[:200],
                )
                return content
            logger.warning("agent.ask ollama_empty_message conversation_id=%s", request.conversation_id)
        except Exception as exc:
            logger.exception(
                "agent.ask ollama_error conversation_id=%s error=%s",
                request.conversation_id,
                exc,
            )
            return self._fallback_answer(request, memory_hits, knowledge_hits)
        logger.warning("agent.ask fallback_after_ollama conversation_id=%s", request.conversation_id)
        return self._fallback_answer(request, memory_hits, knowledge_hits)

    def _format_recent_conversation(self, recent_conversation, current_text: str, limit: int = 6) -> str:
        visible_turns = []
        skipped_current = False
        for item in reversed(recent_conversation):
            if not skipped_current and item.role == "user" and item.message == current_text:
                skipped_current = True
                continue
            visible_turns.append(item)
            if len(visible_turns) >= limit:
                break
        visible_turns.reverse()
        if not visible_turns:
            return ""
        return "\n".join(f"- {item.role}: {item.message}" for item in visible_turns)

    async def _entity_context_hits(self, request: AgentRequest) -> list[str]:
        exposed_entities = request.context_overrides.get("exposed_entities", [])
        if not exposed_entities:
            return []
        text = request.text.lower()
        matched = [entity_id for entity_id in exposed_entities if entity_id.lower().split(".")[-1].replace("_", " ") in text]
        return matched[:5]

    async def _live_state_context_hits(self, request: AgentRequest) -> list[str]:
        if not self._should_fetch_live_state(request):
            return []
        if not self.home_assistant.available:
            return []

        try:
            entities = await self.home_assistant.list_entities()
        except Exception:
            logger.exception("agent.ask home_assistant_live_state_error conversation_id=%s", request.conversation_id)
            return []

        alias_map = self.memory_store.get_device_alias_map()
        relevant = self._select_relevant_entities(request.text, entities, alias_map)
        return [self._format_entity_state(entity, alias_map) for entity in relevant[:8]]

    def _should_fetch_live_state(self, request: AgentRequest) -> bool:
        if request.source != "home_assistant_assist":
            return False
        lowered = request.text.lower()
        keywords = (
            "status",
            "state",
            "currently",
            "right now",
            "temperature",
            "humidity",
            "sensor",
            "light",
            "lamp",
            "switch",
            "scene",
            "climate",
            "thermostat",
            "door",
            "window",
            "motion",
            "presence",
            "camera",
            "battery",
            "power",
            "energy",
            "what is on",
            "what's on",
            "home",
            "house",
            "дім",
            "дом",
            "стан",
            "статус",
            "зараз",
            "увімк",
            "ввімк",
            "включ",
            "виключ",
            "вимк",
            "світ",
            "ламп",
            "темпера",
            "волог",
            "сенсор",
            "датчик",
            "двер",
            "вікн",
            "рух",
            "присут",
            "батар",
            "заряд",
            "енер",
        )
        return any(keyword in lowered for keyword in keywords)

    def _select_relevant_entities(self, text: str, entities: list[dict], alias_map: dict[str, str] | None = None) -> list[dict]:
        alias_map = alias_map or {}
        tokens = [token for token in re.split(r"\W+", text.lower()) if len(token) >= 3]
        matched: list[tuple[int, dict]] = []

        for entity in entities:
            entity_id = entity.get("entity_id", "")
            friendly_name = str(entity.get("attributes", {}).get("friendly_name", ""))
            alias = alias_map.get(entity_id, "")
            haystack = f"{entity_id} {friendly_name} {alias}".lower()
            score = sum(1 for token in tokens if token in haystack)
            if score == 0:
                continue
            matched.append((score, entity))

        if matched:
            matched.sort(key=lambda item: item[0], reverse=True)
            return [entity for _, entity in matched]

        fallback_domains = (
            "light.",
            "switch.",
            "climate.",
            "sensor.",
            "binary_sensor.",
            "cover.",
            "fan.",
            "media_player.",
            "lock.",
        )
        return [entity for entity in entities if any(entity.get("entity_id", "").startswith(domain) for domain in fallback_domains)]

    def _format_entity_state(self, entity: dict[str, object], alias_map: dict[str, str] | None = None) -> str:
        alias_map = alias_map or {}
        entity_id = str(entity.get("entity_id", "unknown"))
        state = str(entity.get("state", "unknown"))
        attributes = entity.get("attributes", {}) if isinstance(entity.get("attributes"), dict) else {}
        friendly_name = str(attributes.get("friendly_name", entity_id))
        preferred_name = alias_map.get(entity_id, friendly_name)

        extras = []
        for key in ("temperature", "current_temperature", "humidity", "brightness", "volume_level", "battery"):
            value = attributes.get(key)
            if value not in (None, "", "unknown", "unavailable"):
                extras.append(f"{key}={value}")

        suffix = f" [{', '.join(extras)}]" if extras else ""
        label = f"{preferred_name} ({entity_id})" if preferred_name != friendly_name else f"{friendly_name} ({entity_id})"
        return f"{label} is {state}{suffix}"

    def _fallback_answer(self, request: AgentRequest, memory_hits, knowledge_hits) -> str:
        if memory_hits or knowledge_hits:
            parts = []
            if memory_hits:
                parts.append("I found related memory context.")
            if knowledge_hits:
                parts.append("I also found relevant local knowledge.")
            return " ".join(parts) + f" You asked: {request.text}"
        return f"I received your request: {request.text}"

    async def _build_action_proposal(self, request: AgentRequest, recent_conversation) -> ActionProposal | None:
        match = self.action_registry.match(request.text)
        if match is not None:
            action_key, definition = match
            reason = f"Matched requested action '{action_key}' from the user message."
            return self._store_action_proposal(
                request=request,
                label=definition.label,
                ha_domain=definition.ha_domain,
                ha_service=definition.ha_service,
                service_data=dict(definition.default_service_data),
                reason=reason,
                risk_level=definition.risk_level,
            )
        return await self._build_generic_device_action_proposal(request, recent_conversation)

    async def _build_generic_device_action_proposal(self, request: AgentRequest, recent_conversation) -> ActionProposal | None:
        intent = self._detect_generic_action_intent(request.text)
        if intent is None or not self.home_assistant.available:
            return None

        service, domains = intent
        try:
            entities = await self.home_assistant.list_entities()
        except Exception:
            logger.exception("agent.action_proposal live_entity_fetch_error conversation_id=%s", request.conversation_id)
            return None

        alias_map = self.memory_store.get_device_alias_map()
        candidate_entities = [
            entity
            for entity in entities
            if any(str(entity.get("entity_id", "")).startswith(f"{domain}.") for domain in domains)
        ]
        if not candidate_entities:
            return None

        entity = self._pick_best_action_entity(request.text, candidate_entities, alias_map)
        if entity is None:
            entity = self._resolve_recent_referenced_entity(request.text, recent_conversation, candidate_entities, alias_map)
        if entity is None:
            return None

        entity_id = str(entity.get("entity_id", ""))
        if "." not in entity_id:
            return None
        domain = entity_id.split(".", 1)[0]
        friendly_name = str(entity.get("attributes", {}).get("friendly_name", entity_id))
        preferred_name = alias_map.get(entity_id, friendly_name)
        action_verb = service.replace("_", " ")
        label = f"{action_verb.title()} {preferred_name}"
        reason = f"Matched requested action to {preferred_name} ({entity_id})."
        return self._store_action_proposal(
            request=request,
            label=label,
            ha_domain=domain,
            ha_service=service,
            service_data={"entity_id": entity_id},
            reason=reason,
            risk_level="medium",
        )

    def _store_action_proposal(
        self,
        request: AgentRequest,
        label: str,
        ha_domain: str,
        ha_service: str,
        service_data: dict[str, Any],
        reason: str,
        risk_level: str,
    ) -> ActionProposal:
        action_id = str(uuid4())
        with self.session_factory() as connection:
            connection.execute(
                """
                INSERT INTO action_traces(
                    action_id, conversation_id, user_id, label, ha_domain, ha_service, service_data_json,
                    reason, risk_level, status, result_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    request.conversation_id,
                    request.user_id,
                    label,
                    ha_domain,
                    ha_service,
                    json.dumps(service_data),
                    reason,
                    risk_level,
                    "pending",
                    "",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

        return ActionProposal(
            action_id=action_id,
            label=label,
            ha_domain=ha_domain,
            ha_service=ha_service,
            service_data=dict(service_data),
            reason=reason,
            risk_level=risk_level,
        )

    def _detect_generic_action_intent(self, text: str) -> tuple[str, tuple[str, ...]] | None:
        lowered = text.casefold()
        service = None
        turn_off_patterns = (
            r"\bturn\b(?:\W+\w+){0,2}\W+\boff\b",
            r"\bswitch\b(?:\W+\w+){0,2}\W+\boff\b",
            r"\bshut\b(?:\W+\w+){0,2}\W+\boff\b",
        )
        turn_on_patterns = (
            r"\bturn\b(?:\W+\w+){0,2}\W+\bon\b",
            r"\bswitch\b(?:\W+\w+){0,2}\W+\bon\b",
        )
        if any(re.search(pattern, lowered) for pattern in turn_off_patterns) or any(
            keyword in lowered for keyword in ("disable", "\u0432\u0438\u043c\u043a", "\u0432\u0438\u043a\u043b", "\u0432\u0456\u0434\u043a\u043b")
        ):
            service = "turn_off"
        elif any(re.search(pattern, lowered) for pattern in turn_on_patterns) or any(
            keyword in lowered for keyword in ("enable", "\u0443\u0432\u0456\u043c\u043a", "\u0432\u0432\u0456\u043c\u043a", "\u0432\u043a\u043b")
        ):
            service = "turn_on"
        elif any(keyword in lowered for keyword in ("toggle", "\u043f\u0435\u0440\u0435\u043c\u043a")):
            service = "toggle"
        if service is None:
            return None

        domain_hints: list[str] = []
        hint_map = (
            ("light", ("light", "lamp", "\u0441\u0432\u0456\u0442", "\u043b\u0430\u043c\u043f")),
            ("switch", ("switch", "outlet", "plug", "\u0440\u043e\u0437\u0435\u0442", "\u0432\u0438\u043c\u0438\u043a\u0430\u0447")),
            ("fan", ("fan", "\u0432\u0435\u043d\u0442\u0438\u043b")),
            ("cover", ("cover", "blind", "curtain", "shade", "\u0448\u0442\u043e\u0440", "\u0436\u0430\u043b\u044e\u0437")),
            ("lock", ("lock", "door lock", "\u0437\u0430\u043c\u043e\u043a")),
            ("media_player", ("tv", "speaker", "music", "media player", "\u0442\u0435\u043b\u0435\u0432\u0456\u0437", "\u043a\u043e\u043b\u043e\u043d\u043a", "\u043c\u0443\u0437\u0438\u043a")),
        )
        for domain, keywords in hint_map:
            if any(keyword in lowered for keyword in keywords):
                domain_hints.append(domain)

        return service, tuple(domain_hints or ("light", "switch", "fan", "cover", "lock", "media_player", "input_boolean"))

    def _pick_best_action_entity(
        self,
        text: str,
        candidates: list[dict[str, Any]],
        alias_map: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        alias_map = alias_map or {}
        generic_tokens = {
            "turn",
            "on",
            "off",
            "switch",
            "shut",
            "disable",
            "enable",
            "toggle",
            "light",
            "lamp",
            "fan",
            "cover",
            "blind",
            "curtain",
            "shade",
            "lock",
            "door",
            "tv",
            "speaker",
            "music",
            "plug",
            "outlet",
            "media",
            "player",
            "home",
            "house",
            "\u0432\u0438\u043c\u043a\u043d\u0438",
            "\u0432\u0438\u043a\u043b\u044e\u0447\u0438",
            "\u0432\u0438\u043c\u043a\u043d\u0443\u0442\u0438",
            "\u0432\u0438\u043a\u043d\u0443\u0442\u0438",
            "\u0443\u0432\u0456\u043c\u043a\u043d\u0438",
            "\u0432\u043a\u043b\u044e\u0447\u0438",
            "\u043f\u0435\u0440\u0435\u043c\u043a\u043d\u0438",
            "\u0441\u0432\u0456\u0442\u043b\u043e",
            "\u043b\u0430\u043c\u043f\u0430",
            "\u0432\u0435\u043d\u0442\u0438\u043b\u044f\u0442\u043e\u0440",
            "\u0448\u0442\u043e\u0440\u0430",
            "\u0436\u0430\u043b\u044e\u0437\u0456",
            "\u0437\u0430\u043c\u043e\u043a",
            "\u0440\u043e\u0437\u0435\u0442\u043a\u0430",
            "\u0432\u0438\u043c\u0438\u043a\u0430\u0447",
        }
        tokens = [
            token
            for token in re.split(r"\W+", text.casefold())
            if len(token) >= 3 and token not in generic_tokens
        ]
        scored: list[tuple[int, dict[str, Any]]] = []
        for entity in candidates:
            entity_id = str(entity.get("entity_id", ""))
            friendly_name = str(entity.get("attributes", {}).get("friendly_name", ""))
            alias = alias_map.get(entity_id, "")
            haystack = f"{entity_id} {friendly_name} {alias}".casefold()
            score = sum(1 for token in tokens if token in haystack)
            scored.append((score, entity))

        scored.sort(key=lambda item: item[0], reverse=True)
        if scored and scored[0][0] > 0:
            if len(scored) > 1 and scored[0][0] == scored[1][0]:
                return None
            return scored[0][1]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _resolve_recent_referenced_entity(
        self,
        text: str,
        recent_conversation,
        candidates: list[dict[str, Any]],
        alias_map: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        alias_map = alias_map or {}
        if not self._looks_like_followup_reference(text):
            return None

        recent_messages = []
        skipped_current = False
        for item in reversed(recent_conversation):
            if not skipped_current and item.role == "user" and item.message == text:
                skipped_current = True
                continue
            recent_messages.append(item.message.casefold())

        for message in recent_messages:
            for entity in candidates:
                entity_id = str(entity.get("entity_id", ""))
                friendly_name = str(entity.get("attributes", {}).get("friendly_name", ""))
                alias = alias_map.get(entity_id, "")
                names = [entity_id.casefold(), friendly_name.casefold(), alias.casefold()]
                names = [name for name in names if name]
                if any(name in message for name in names):
                    return entity
        return None

    def _looks_like_followup_reference(self, text: str) -> bool:
        lowered = text.casefold()
        followup_markers = (
            " it",
            " this",
            " that",
            " him",
            " her",
            " \u0439\u043e\u0433\u043e",
            " \u0457\u0457",
            " \u0446\u0435",
            " \u0446\u0435\u0439",
            " \u0446\u044e",
            " \u0442\u043e\u0439",
            " \u0442\u0443",
            " \u043d\u044c\u043e\u0433\u043e",
            " \u043d\u0435\u0457",
        )
        if any(marker in f" {lowered} " for marker in followup_markers):
            return True
        tokens = [token for token in re.split(r"\W+", lowered) if token]
        generic_only = {
            "turn",
            "on",
            "off",
            "switch",
            "shut",
            "disable",
            "enable",
            "toggle",
            "uvimkni",
            "\u0443\u0432\u0456\u043c\u043a\u043d\u0438",
            "\u0443\u0432\u0456\u043c\u043a\u043d\u0443\u0442\u0438",
            "\u0432\u0432\u0456\u043c\u043a\u043d\u0438",
            "\u0432\u0432\u0456\u043c\u043a\u043d\u0443\u0442\u0438",
            "\u0432\u043a\u043b\u044e\u0447\u0438",
            "\u0432\u043a\u043b\u044e\u0447\u0438\u0442\u0438",
            "vimkni",
            "\u0432\u0438\u043c\u043a\u043d\u0438",
            "\u0432\u0438\u043c\u043a\u043d\u0443\u0442\u0438",
            "\u0432\u0456\u0434\u043a\u043b\u044e\u0447\u0438",
            "\u043f\u0435\u0440\u0435\u043c\u043a\u043d\u0438",
        }
        return bool(tokens) and all(token in generic_only for token in tokens)

    async def _deterministic_home_assistant_answer(self, request: AgentRequest) -> str | None:
        if not self._is_inventory_intent(request):
            return None
        if not self.home_assistant.available:
            return "Home Assistant зараз недоступний, тому я не можу зібрати перелік сутностей."

        try:
            entities = await self.home_assistant.list_entities()
        except Exception:
            logger.exception("agent.ask inventory_list_error conversation_id=%s", request.conversation_id)
            return "Не вдалося отримати поточний перелік сутностей з Home Assistant."

        alias_map = self.memory_store.get_device_alias_map()
        grouped = self._group_inventory_entities(entities)
        total = sum(len(items) for items in grouped.values())
        if total == 0:
            return "З Home Assistant не повернулося жодної сутності у вибраних доменах."

        top_domains = sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)[:8]
        lines = [f"Я бачу {total} сутностей Home Assistant у {len(grouped)} доменах."]
        lines.append("")
        lines.append("Найбільші групи:")
        for domain, items in top_domains:
            lines.append(f"- {domain}: {len(items)}")

        examples: list[str] = []
        for domain, items in top_domains[:3]:
            for entity in items:
                formatted = self._format_entity_state(entity, alias_map)
                examples.append(f"{domain}: {formatted}")
                if len(examples) >= 3:
                    break
            if len(examples) >= 3:
                break

        if examples:
            lines.append("")
            lines.append("Приклади:")
            for example in examples:
                lines.append(f"- {example}")

        lines.append("")
        lines.append("Можу показати детальніше за доменом: світло, сенсори, скрипти, автоматизації, камери, клімат.")
        return "\n".join(lines)

    def _is_inventory_intent(self, request: AgentRequest) -> bool:
        if request.source != "home_assistant_assist":
            return False
        lowered = request.text.casefold()
        phrases = (
            "all devices",
            "all entities",
            "what do you see",
            "list everything",
            "list all",
            "\u0432\u0441\u0456 \u043f\u0440\u0438\u0441\u0442\u0440\u043e\u0457",
            "\u0443\u0441\u0456 \u043f\u0440\u0438\u0441\u0442\u0440\u043e\u0457",
            "\u0432\u0441\u0456 \u0441\u0443\u0442\u043d\u043e\u0441\u0442\u0456",
            "\u0443\u0441\u0456 \u0441\u0443\u0442\u043d\u043e\u0441\u0442\u0456",
            "\u0434\u0430\u0439 \u043f\u0435\u0440\u0435\u043b\u0456\u043a",
            "\u043f\u043e\u043a\u0430\u0436\u0438 \u0432\u0441\u0435",
            "\u0449\u043e \u0442\u0438 \u0431\u0430\u0447\u0438\u0448",
            "\u0432\u0435\u0441\u044c \u0441\u043f\u0438\u0441\u043e\u043a",
            "\u043f\u0435\u0440\u0435\u043b\u0456\u043a \u043f\u0440\u0438\u0441\u0442\u0440\u043e\u0457\u0432",
            "\u043f\u0440\u0438\u0441\u0442\u0440\u043e\u0457 \u0432 \u0434\u043e\u043c\u0456",
            "\u043f\u0440\u0438\u0441\u0442\u0440\u043e\u0457 \u0434\u043e\u043c\u0443",
        )
        return any(phrase in lowered for phrase in phrases)

    def _group_inventory_entities(self, entities: list[dict]) -> dict[str, list[dict]]:
        allowed_domains = (
            "automation",
            "script",
            "scene",
            "light",
            "switch",
            "sensor",
            "binary_sensor",
            "climate",
            "cover",
            "fan",
            "media_player",
            "lock",
            "input_boolean",
            "person",
            "device_tracker",
            "camera",
            "vacuum",
            "button",
            "select",
            "number",
        )
        grouped: dict[str, list[dict]] = {}
        for entity in entities:
            entity_id = str(entity.get("entity_id", ""))
            if "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain not in allowed_domains:
                continue
            grouped.setdefault(domain, []).append(entity)
        for items in grouped.values():
            items.sort(key=lambda entity: str(entity.get("attributes", {}).get("friendly_name", entity.get("entity_id", ""))).casefold())
        return dict(sorted(grouped.items(), key=lambda item: item[0]))

    async def _extract_and_store_memory(self, request: AgentRequest) -> list[int]:
        source_ref = f"conversation:{request.conversation_id}"
        extracted = self.memory_store.extract_facts_from_text(request.text, source_ref=source_ref)
        llm_facts = await self._extract_memory_with_model(request.text)
        for fact in llm_facts:
            stored_id = self.memory_store.add_fact(source_ref=source_ref, **fact)
            extracted.append(stored_id)
        return extracted

    async def _extract_memory_with_model(self, text: str) -> list[dict[str, Any]]:
        if not self.ollama.available:
            return []
        if not self._should_attempt_memory_extraction(text):
            return []

        prompt = (
            "You extract long-term memory facts from user messages for a local personal assistant.\n"
            "Return only valid JSON.\n"
            "Output either [] or an array of objects with keys: "
            "category, subject, predicate, value, confidence, importance, tags.\n"
            "Rules:\n"
            "- Keep only durable or reusable facts worth remembering.\n"
            "- Allowed categories: preference, profile, routine, rule, relationship, project, secret, credential, device_alias.\n"
            "- If the user assigns a human-friendly name to a Home Assistant entity, use category=device_alias, predicate=alias, subject=exact entity_id.\n"
            "- confidence is a float from 0.0 to 1.0.\n"
            "- importance is an integer from 1 to 10.\n"
            "- tags must be an array of short strings.\n"
            "- Do not include explanations or markdown fences.\n"
            "- If there are no memory-worthy facts, return [].\n"
            f"User message: {json.dumps(text, ensure_ascii=False)}"
        )
        messages = [
            {"role": "system", "content": "You are a strict JSON information extractor."},
            {"role": "user", "content": prompt},
        ]
        try:
            payload = await self.ollama.chat(messages)
            content = payload.get("message", {}).get("content", "").strip()
            return self._parse_memory_extraction_json(content)
        except Exception:
            logger.exception("agent.memory_extraction_error")
            return []

    def _should_attempt_memory_extraction(self, text: str) -> bool:
        normalized = text.strip()
        if len(normalized) < 12:
            return False
        lowered = normalized.casefold()
        if "?" in normalized:
            return False
        if lowered in {"hi", "hello", "ok", "thanks", "thank you", "дякую", "привіт", "привет", "як справи"}:
            return False
        noisy_markers = (
            "http://",
            "https://",
            "/confirm ",
            "/cancel ",
            "weather",
            "погода",
            "status",
            "state",
            "currently",
            "right now",
            "what do you see",
            "list everything",
            "list all",
            "всі пристрої",
            "усі пристрої",
            "всі сутності",
            "усі сутності",
            "дай перелік",
            "покажи все",
            "що ти бачиш",
            "температур",
            "світло",
            "sensor",
            "light.",
            "switch.",
            "scene.",
            "script.",
        )
        if any(marker in lowered for marker in noisy_markers):
            return False
        memory_markers = (
            "remember",
            "note that",
            "i prefer",
            "my ",
            "мене звати",
            "я люблю",
            "мені подобається",
            "я надаю перевагу",
            "запам'ятай",
            "запам’ятай",
            "запиши",
            "нотатка:",
            "у мене",
            "я працюю",
            "я живу",
            "для мене важливо",
            "це ",
            " is ",
            " means ",
        )
        return any(marker in lowered for marker in memory_markers)
        lowered = normalized.lower()
        if lowered in {"hi", "hello", "ok", "thanks", "дякую", "привіт"}:
            return False
        noisy_markers = ("http://", "https://", "/confirm ", "/cancel ", "weather", "погода")
        if any(marker in lowered for marker in noisy_markers):
            return False
        return True

    def _parse_memory_extraction_json(self, content: str) -> list[dict[str, Any]]:
        if not content:
            return []
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("agent.memory_extraction_invalid_json content=%r", cleaned[:300])
            return []
        if not isinstance(payload, list):
            return []

        allowed_categories = {"preference", "profile", "routine", "rule", "relationship", "project", "secret", "credential", "device_alias"}
        results: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "")).strip().lower()
            subject = str(item.get("subject", "")).strip()
            predicate = str(item.get("predicate", "")).strip()
            value = str(item.get("value", "")).strip().rstrip(".")
            if category not in allowed_categories or not subject or not predicate or len(value) < 2:
                continue
            try:
                confidence = float(item.get("confidence", 0.75))
            except (TypeError, ValueError):
                confidence = 0.75
            try:
                importance = int(item.get("importance", 6))
            except (TypeError, ValueError):
                importance = 6
            raw_tags = item.get("tags", [])
            tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
            results.append(
                {
                    "category": category,
                    "subject": subject[:120],
                    "predicate": predicate[:120],
                    "value": value[:500],
                    "confidence": min(max(confidence, 0.0), 1.0),
                    "importance": min(max(importance, 1), 10),
                    "tags": tags[:8],
                }
            )
        return results

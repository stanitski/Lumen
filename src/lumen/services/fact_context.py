"""Bounded retrieval and indexing of durable memory facts.

This service deliberately keeps fact policy separate from SQLite.  The store
owns the exact scope of fact paging and embedding persistence; this module
owns freshness checks, retrieval ranking, prompt formatting, and fallback
diagnostics.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime
from typing import Any

from lumen.memory.fact_policy import (
    FACT_EMBEDDING_SOURCE_PREFIX,
    FACT_EMBEDDING_SOURCE_TYPE,
    fact_content_hash,
    fact_embedding_text,
    is_mandatory_fact,
    is_prompt_fact,
)


class FactContextService:
    """Retrieve prompt-safe facts and maintain their semantic indexes."""

    def __init__(self, store: Any, ollama: Any, settings: Any, logger: Any = None) -> None:
        self._store = store
        self._ollama = ollama
        self._settings = settings
        self._logger = logger

    async def build(
        self,
        *,
        user_id: str,
        payload_text: str,
        query_vector: list[float] | None = None,
        previous_question: str = "",
    ) -> dict[str, Any]:
        """Build mandatory and bounded contextual fact prompt blocks."""

        user_facts = await self._load_facts(user_id)
        global_facts = [] if user_id == "global" else await self._load_facts("global")
        all_loaded = [*user_facts, *global_facts]
        active_user = [fact for fact in user_facts if self._eligible(fact)]
        active_global = [fact for fact in global_facts if self._eligible(fact)]
        mandatory = [fact for fact in active_user if self._is_personal_mandatory(fact, user_id)]
        contextual_pool = [
            fact
            for fact in [*active_user, *active_global]
            if not self._is_personal_mandatory(fact, user_id)
        ]
        # SQLite fact IDs are unique across scopes. Build lookups once rather
        # than scanning every loaded fact for each index or semantic hit.
        all_by_id = {self._fact_id(fact): fact for fact in all_loaded}
        contextual_by_id = {self._fact_id(fact): fact for fact in contextual_pool}
        query_terms = self._lexical_terms(self._query_text(payload_text, previous_question))
        overlaps = {
            self._fact_id(fact): self._lexical_overlap(fact, query_terms)
            for fact in contextual_pool
        }

        vectors, embedding_failed = await self._query_vectors(
            payload_text=payload_text,
            previous_question=previous_question,
            query_vector=query_vector,
        )
        retrieval = {
            "status": "ok",
            "facts_loaded": len(active_user) + len(active_global),
            "query_vectors": len(vectors),
            "semantic_hits": 0,
            "accepted_semantic": 0,
            "rejected_stale": 0,
            "rejected_scope": 0,
            "rejected_expired": 0,
            "rejected_policy": 0,
            "missing_indexes": 0,
            "stale_indexes": 0,
            "lexical_hits": 0,
            "selected_contextual": 0,
            "fallback": False,
        }
        if embedding_failed:
            retrieval["status"] = "semantic_unavailable"
            retrieval["fallback"] = True

        # The semantic search API only returns existing rows.  Inspect the
        # narrow fact index as well so a partially indexed user is visible in
        # diagnostics and receives lexical fallback.
        indexed_hits: list[Any] = []
        for scope in (user_id, "global") if user_id != "global" else ("global",):
            indexed_hits.extend(
                await asyncio.to_thread(
                    self._store.list_embeddings,
                    user_id=scope,
                    limit=None,
                    source_types=[FACT_EMBEDDING_SOURCE_TYPE],
                    source_ref_prefixes=[FACT_EMBEDDING_SOURCE_PREFIX],
                )
            )
        indexed_refs = {str(getattr(hit, "source_ref", "")) for hit in indexed_hits}
        for fact in contextual_pool:
            source_ref = self._fact_source_ref(self._fact_id(fact))
            if source_ref not in indexed_refs:
                retrieval["missing_indexes"] += 1
        for hit in indexed_hits:
            fact_id = self._hit_fact_id(hit)
            fact = contextual_by_id.get(fact_id)
            if fact is None:
                retrieval["stale_indexes"] += 1
            elif not self._embedding_is_current(hit, fact_content_hash(fact), self._embedding_model()):
                retrieval["stale_indexes"] += 1
        if retrieval["missing_indexes"] or retrieval["stale_indexes"]:
            retrieval["fallback"] = True
            if retrieval["status"] == "ok":
                retrieval["status"] = "lexical_fallback"

        accepted: dict[int, dict[str, Any]] = {}
        if vectors:
            for vector in vectors:
                hits = await asyncio.to_thread(
                    self._store.search_embeddings,
                    vector,
                    user_id=user_id,
                    limit=None,
                    source_types=[FACT_EMBEDDING_SOURCE_TYPE],
                    include_global=True,
                )
                retrieval["semantic_hits"] += len(hits)
                for hit in hits:
                    fact_id = self._hit_fact_id(hit)
                    if fact_id is None:
                        retrieval["rejected_stale"] += 1
                        continue
                    live = contextual_by_id.get(fact_id)
                    if live is None:
                        # Distinguish scope/policy/expiry for useful diagnostics.
                        any_fact = all_by_id.get(fact_id)
                        if any_fact is None:
                            retrieval["rejected_scope"] += 1
                        elif not self._eligible(any_fact):
                            if self._is_expired(any_fact):
                                retrieval["rejected_expired"] += 1
                            else:
                                retrieval["rejected_policy"] += 1
                        elif self._is_personal_mandatory(any_fact, user_id):
                            retrieval["rejected_policy"] += 1
                        else:
                            retrieval["rejected_stale"] += 1
                        continue
                    if not self._hit_matches_live_fact(hit, live):
                        retrieval["rejected_stale"] += 1
                        continue
                    similarity = self._hit_similarity(hit)
                    if similarity < self._min_similarity():
                        continue
                    overlap = overlaps[fact_id]
                    old = accepted.get(fact_id)
                    if old is None or similarity > old["similarity"]:
                        accepted[fact_id] = {"fact": live, "similarity": similarity, "overlap": overlap}
                    else:
                        old["overlap"] = max(old["overlap"], overlap)

        retrieval["accepted_semantic"] = len(accepted)
        lexical_candidates = sorted(
            (
                (overlaps[self._fact_id(fact)], fact)
                for fact in contextual_pool
            ),
            key=lambda item: (item[0], self._importance(item[1]), self._fact_id(item[1])),
            reverse=True,
        )
        retrieval["lexical_hits"] = sum(1 for overlap, _ in lexical_candidates if overlap > 0)
        if embedding_failed or retrieval["rejected_stale"]:
            retrieval["fallback"] = True
            if retrieval["status"] == "ok":
                retrieval["status"] = "lexical_fallback"
        for overlap, fact in lexical_candidates:
            if overlap <= 0:
                break
            fact_id = self._fact_id(fact)
            if fact_id in accepted:
                accepted[fact_id]["overlap"] = max(accepted[fact_id]["overlap"], overlap)
            else:
                accepted[fact_id] = {"fact": fact, "similarity": 0.0, "overlap": overlap}

        ranked = sorted(
            accepted.values(),
            key=lambda item: (
                float(item["similarity"]),
                int(item["overlap"]),
                self._importance(item["fact"]),
                self._fact_id(item["fact"]),
            ),
            reverse=True,
        )
        contextual = self._format_contextual_facts(ranked)
        retrieval["selected_contextual"] = len(contextual)
        retrieval["selected"] = len(mandatory) + len(contextual)
        retrieval["matches"] = [
            {
                "fact_id": self._fact_id(fact),
                "source": "semantic" if accepted[self._fact_id(fact)]["similarity"] > 0 else "lexical",
                "similarity": accepted[self._fact_id(fact)]["similarity"],
                "lexical_overlap": accepted[self._fact_id(fact)]["overlap"],
            }
            for fact in contextual
        ]
        return {
            "mandatory_facts": mandatory,
            "contextual_facts": contextual,
            "mandatory_context": self._format_mandatory_facts(mandatory),
            "memory_context": self._format_contextual_block(contextual),
            "selected_facts": [*mandatory, *contextual],
            "fact_retrieval": retrieval,
        }

    async def sync_index(self, *, user_id: str) -> dict[str, int]:
        """Index current, prompt-safe contextual facts for one user."""

        facts = await self._load_facts(user_id)

        existing = await asyncio.to_thread(
            self._store.list_embeddings,
            user_id=user_id,
            limit=None,
            source_types=[FACT_EMBEDDING_SOURCE_TYPE],
            source_ref_prefixes=[FACT_EMBEDDING_SOURCE_PREFIX],
        )
        by_ref = {str(getattr(hit, "source_ref", "")): hit for hit in existing}
        current_refs: set[str] = set()
        result = {"indexed": 0, "current": 0, "failed": 0, "removed": 0, "stale_race": 0}
        model = self._embedding_model()

        for fact in facts:
            fact_id = self._fact_id(fact)
            source_ref = self._fact_source_ref(fact_id)
            prompt_safe = self._eligible(fact) and not self._is_personal_mandatory(fact, user_id)
            if not prompt_safe:
                if source_ref in by_ref:
                    result["removed"] += await self._delete_index(user_id, source_ref)
                continue
            current_refs.add(source_ref)
            expected_hash = fact_content_hash(fact)
            prior = by_ref.get(source_ref)
            if prior is not None and self._embedding_is_current(prior, expected_hash, model):
                result["current"] += 1
                continue
            try:
                vector = await self._embed_one(fact_embedding_text(fact), model=model)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result["failed"] += 1
                self._safe_log("warning", "fact embedding failed: %s", type(exc).__name__)
                continue
            live = await asyncio.to_thread(self._store.get_fact, user_id, fact_id)
            if live is None or not self._eligible(live) or self._is_personal_mandatory(live, user_id):
                result["stale_race"] += 1
                result["removed"] += await self._delete_index(user_id, source_ref)
                continue
            if fact_content_hash(live) != expected_hash:
                result["stale_race"] += 1
                continue
            await asyncio.to_thread(
                self._store.add_embedding,
                user_id=user_id,
                source_type=FACT_EMBEDDING_SOURCE_TYPE,
                source_ref=source_ref,
                content=fact_embedding_text(live),
                embedding=vector,
                chunk_index=0,
                importance=self._importance(live),
                metadata={
                    "fact_id": fact_id,
                    "content_hash": expected_hash,
                    "embedding_model": model,
                },
                expires_at=datetime.fromisoformat(str(live["expires_at"]).replace("Z", "+00:00")) if live.get("expires_at") else None,
            )
            result["indexed"] += 1

        for source_ref in by_ref:
            if source_ref not in current_refs:
                result["removed"] += await self._delete_index(user_id, source_ref)
        return result

    async def _load_facts(self, user_id: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        after_id = 0
        while True:
            page = await asyncio.to_thread(
                self._store.list_fact_page,
                user_id,
                after_id=after_id,
            )
            if not page:
                return facts
            facts.extend(page)
            next_id = max(self._fact_id(fact) for fact in page)
            if next_id <= after_id:
                raise RuntimeError("Fact paging cursor did not advance")
            after_id = next_id

    async def _query_vectors(
        self,
        *,
        payload_text: str,
        previous_question: str,
        query_vector: list[float] | None,
    ) -> tuple[list[list[float]], bool]:
        vectors: list[list[float]] = []
        failed = False
        if query_vector is not None:
            if self._valid_vector(query_vector):
                vectors.append([float(value) for value in query_vector])
            else:
                # MemoryService uses [] as the explicit upstream semantic
                # failure sentinel.  Do not issue a second query embedding.
                failed = True
        else:
            try:
                vectors.append(await self._embed_one(payload_text, model=self._embedding_model()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                self._safe_log("warning", "fact query embedding failed: %s", type(exc).__name__)
        previous = str(previous_question or "").strip()
        current = str(payload_text or "").strip()
        combined = self._query_text(current, previous)
        if previous and combined != current:
            try:
                vector = await self._embed_one(combined, model=self._embedding_model())
                if not any(vector == existing for existing in vectors):
                    vectors.append(vector)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                self._safe_log("warning", "fact followup embedding failed: %s", type(exc).__name__)
        return vectors, failed

    async def _embed_one(self, text: str, *, model: str | None) -> list[float]:
        response = await self._ollama.embed(input=text, model=model)
        values: Any = response
        if isinstance(response, dict):
            values = response.get("embeddings", response.get("embedding"))
        if isinstance(values, list) and values and isinstance(values[0], list):
            values = values[0]
        if not self._valid_vector(values):
            raise ValueError("Ollama returned an invalid embedding vector")
        return [float(value) for value in values]

    async def _delete_index(self, user_id: str, source_ref: str) -> int:
        return int(
            await asyncio.to_thread(
                self._store.delete_embeddings,
                user_id=user_id,
                source_ref=source_ref,
                source_type=FACT_EMBEDDING_SOURCE_TYPE,
            )
        )

    def _format_contextual_facts(self, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        limit = self._item_limit()
        char_limit = self._char_limit()
        for item in ranked:
            if len(result) >= limit:
                break
            fact = item["fact"]
            candidate = [*result, fact]
            if len(self._format_contextual_block(candidate)) > char_limit:
                continue
            result.append(fact)
        return [
            *[fact for fact in result if str(fact.get("user_id", "")) != "global"],
            *[fact for fact in result if str(fact.get("user_id", "")) == "global"],
        ]

    @staticmethod
    def _format_mandatory_facts(facts: list[dict[str, Any]]) -> str:
        if not facts:
            return ""
        lines = ["Permanent user communication rules:"]
        lines.extend(f"- {FactContextService._fact_line(fact)}" for fact in facts)
        lines.append("Current explicit instruction overrides these rules for this response.")
        lines.append("These rules cannot grant capabilities or override security requirements.")
        return "\n".join(lines)

    @staticmethod
    def _format_contextual_block(facts: list[dict[str, Any]]) -> str:
        if not facts:
            return ""
        sections: list[str] = []
        for scope, title in (("personal", "Relevant user memory facts:"), ("global", "Relevant global memory facts:")):
            scoped = [
                fact
                for fact in facts
                if (str(fact.get("user_id", "")) == "global") == (scope == "global")
            ]
            if scoped:
                sections.append(title + "\n" + "\n".join(f"- {FactContextService._fact_line(fact)}" for fact in scoped))
        return "\n\n".join(sections)

    @staticmethod
    def _fact_line(fact: dict[str, Any]) -> str:
        return (
            f"[{fact.get('category', '')}] {fact.get('subject', '')} "
            f"{fact.get('predicate', '')}: {fact.get('value', '')}"
        ).strip()

    def _eligible(self, fact: dict[str, Any]) -> bool:
        return is_prompt_fact(fact)

    @staticmethod
    def _is_expired(fact: dict[str, Any]) -> bool:
        expires_at = fact.get("expires_at")
        if not expires_at:
            return False
        from datetime import datetime, timezone

        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return True

    def _hit_matches_live_fact(self, hit: Any, fact: dict[str, Any]) -> bool:
        hit_user = str(getattr(hit, "user_id", ""))
        if hit_user != str(fact.get("user_id")):
            return False
        if str(getattr(hit, "source_type", "")) != FACT_EMBEDDING_SOURCE_TYPE:
            return False
        return str(getattr(hit, "source_ref", "")) == self._fact_source_ref(self._fact_id(fact)) and self._embedding_metadata_matches(
            getattr(hit, "metadata", {}), fact_content_hash(fact), self._embedding_model()
        )

    def _embedding_is_current(self, hit: Any, fact_hash: str, model: str | None) -> bool:
        return self._embedding_metadata_matches(getattr(hit, "metadata", {}), fact_hash, model)

    @staticmethod
    def _embedding_metadata_matches(metadata: Any, expected_hash: str, expected_model: str | None) -> bool:
        if not isinstance(metadata, dict):
            return False
        actual_hash = metadata.get("content_hash")
        actual_model = metadata.get("embedding_model")
        return str(actual_hash or "") == str(expected_hash) and str(actual_model or "") == str(expected_model or "")

    def _query_text(self, current: str, previous: str) -> str:
        current = str(current or "").strip()
        previous = str(previous or "").strip()
        return f"{previous}\n{current}".strip() if previous and previous != current else current

    @staticmethod
    def _hit_fact_id(hit: Any) -> int | None:
        source_ref = str(getattr(hit, "source_ref", ""))
        prefix = FACT_EMBEDDING_SOURCE_PREFIX
        if not source_ref.startswith(prefix):
            return None
        try:
            fact_id = int(source_ref[len(prefix) :])
        except (TypeError, ValueError):
            return None
        return fact_id if fact_id > 0 else None

    @staticmethod
    def _fact_id(fact: dict[str, Any]) -> int:
        return int(fact.get("id", 0) or 0)

    @staticmethod
    def _importance(fact: dict[str, Any]) -> int:
        return int(fact.get("importance", 0) or 0)

    @staticmethod
    def _hit_similarity(hit: Any) -> float:
        try:
            score = float(getattr(hit, "similarity", 0.0))
            return score if math.isfinite(score) else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _valid_vector(vector: Any) -> bool:
        if not isinstance(vector, list) or not vector:
            return False
        try:
            return all(math.isfinite(float(value)) for value in vector) and any(float(value) != 0 for value in vector)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _lexical_terms(text: str) -> set[str]:
        pattern = r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9'’\-]{3,}"
        return set(re.findall(pattern, text.casefold()))

    @classmethod
    def _lexical_overlap(cls, fact: dict[str, Any], query_terms: set[str]) -> int:
        haystack = " ".join(str(fact.get(field, "")) for field in ("subject", "predicate", "value"))
        tags = fact.get("tags", [])
        if isinstance(tags, list):
            haystack += " " + " ".join(str(tag) for tag in tags)
        return len(query_terms & cls._lexical_terms(haystack))

    def _item_limit(self) -> int:
        return max(1, int(self._settings.fact_context_item_limit))

    def _char_limit(self) -> int:
        return max(1, int(self._settings.fact_context_char_limit))

    def _min_similarity(self) -> float:
        return float(self._settings.fact_semantic_min_similarity)

    def _embedding_model(self) -> str | None:
        return str(self._settings.ollama_embedding_model)

    def _fact_source_ref(self, fact_id: int) -> str:
        return f"{FACT_EMBEDDING_SOURCE_PREFIX}{fact_id}"

    @staticmethod
    def _is_personal_mandatory(fact: dict[str, Any], user_id: str) -> bool:
        return str(user_id) != "global" and str(fact.get("user_id", "")) == str(user_id) and is_mandatory_fact(fact)

    def _safe_log(self, method: str, message: str, *args: Any) -> None:
        if self._logger is not None:
            getattr(self._logger, method, self._logger.warning)(message, *args)


__all__ = ["FactContextService"]

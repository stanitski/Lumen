"""SQLite-backed persistence layer for turns, facts, and embeddings."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from lumen.memory.fact_policy import (
    FACT_EMBEDDING_SOURCE_PREFIX,
    FACT_EMBEDDING_SOURCE_TYPE,
    MANDATORY_TAG,
    fact_embedding_text,
    fact_revision,
    is_mandatory_fact,
)
from lumen.models import (
    ConversationTurnRecord,
    EmbeddingHit,
    KnowledgeUploadSessionRecord,
)
from lumen.time_utils import local_now_iso


class MemoryStore:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def add_embedding(
        self,
        *,
        user_id: str,
        source_type: str,
        source_ref: str,
        content: str,
        embedding: list[float],
        chunk_index: int = 0,
        importance: int = 5,
        created_at: str | None = None,
        metadata: dict[str, object] | None = None,
        embedding_model: str | None = None,
        expires_at: datetime | None = None,
    ) -> int:
        now = local_now_iso()
        created_at_value = created_at or now
        expires_at_iso = expires_at.isoformat() if expires_at else None
        embedding_json = json.dumps([float(value) for value in embedding], ensure_ascii=False)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._session_factory() as connection:
            existing = connection.execute(
                """
                SELECT id
                FROM memory_embeddings
                WHERE user_id = ? AND source_type = ? AND source_ref = ? AND chunk_index = ?
                LIMIT 1
                """,
                (user_id, source_type, source_ref, chunk_index),
            ).fetchone()

            if existing:
                connection.execute(
                    """
                    UPDATE memory_embeddings
                    SET content = ?, embedding_json = ?, importance = ?, metadata_json = ?, expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        content,
                        embedding_json,
                        importance,
                        metadata_json,
                        expires_at_iso,
                        existing["id"],
                    ),
                )
                connection.commit()
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO memory_embeddings(
                    user_id, source_type, source_ref, chunk_index, content, embedding_json, importance, created_at, metadata_json, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source_type,
                    source_ref,
                    chunk_index,
                    content,
                    embedding_json,
                    importance,
                    created_at_value,
                    metadata_json,
                    expires_at_iso,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def replace_embeddings(
        self,
        *,
        user_id: str,
        source_type: str,
        source_ref: str,
        chunks: list[tuple[str, list[float], dict[str, object]]],
        importance: int = 5,
    ) -> int:
        """Replace one document atomically; serialize all chunks before deletion."""
        created_at = local_now_iso()
        rows = [
            (
                user_id, source_type, source_ref, index, content,
                json.dumps(vector, ensure_ascii=False, allow_nan=False),
                importance, created_at,
                json.dumps(metadata, ensure_ascii=False, allow_nan=False),
            )
            for index, (content, vector, metadata) in enumerate(chunks)
        ]
        with self._session_factory() as connection:
            with connection:
                connection.execute(
                    "DELETE FROM memory_embeddings WHERE user_id = ? AND source_type = ? AND source_ref = ?",
                    (user_id, source_type, source_ref),
                )
                connection.executemany(
                    """
                    INSERT INTO memory_embeddings(
                        user_id, source_type, source_ref, chunk_index, content,
                        embedding_json, importance, created_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        return len(rows)

    def delete_embeddings(self, *, user_id: str, source_ref: str, source_type: str | None = None) -> int:
        with self._session_factory() as connection:
            if source_type:
                cursor = connection.execute(
                    """
                    DELETE FROM memory_embeddings
                    WHERE user_id = ? AND source_type = ? AND source_ref = ?
                    """,
                    (user_id, source_type, source_ref),
                )
            else:
                cursor = connection.execute(
                    """
                    DELETE FROM memory_embeddings
                    WHERE user_id = ? AND source_ref = ?
                    """,
                    (user_id, source_ref),
                )
            connection.commit()
            return int(cursor.rowcount)

    def delete_embedding_ids(self, *, user_id: str, embedding_ids: list[int]) -> int:
        ids = [int(item) for item in embedding_ids if int(item) > 0]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._session_factory() as connection:
            cursor = connection.execute(
                f"DELETE FROM memory_embeddings WHERE user_id = ? AND id IN ({placeholders})",
                [user_id, *ids],
            )
            connection.commit()
            return int(cursor.rowcount)

    def get_embedding(self, *, user_id: str, embedding_id: int) -> EmbeddingHit | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source_type, source_ref, chunk_index, content, importance, created_at, metadata_json, embedding_json, expires_at
                FROM memory_embeddings
                WHERE user_id = ? AND id = ?
                """,
                (user_id, int(embedding_id)),
            ).fetchone()
        if row is None:
            return None
        return EmbeddingHit(
            id=int(row["id"]),
            user_id=row["user_id"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            chunk_index=int(row["chunk_index"]),
            content=row["content"],
            importance=int(row["importance"]),
            similarity=0.0,
            created_at=row["created_at"],
            metadata=self._load_json_object(row["metadata_json"]),
            expires_at=row["expires_at"],
        )

    def list_embeddings(
        self,
        *,
        user_id: str,
        limit: int | None = 100,
        source_types: list[str] | None = None,
        source_ref_prefixes: list[str] | None = None,
    ) -> list[EmbeddingHit]:
        safe_limit = max(1, min(int(limit), 1000)) if limit is not None else None
        sql = """
            SELECT id, user_id, source_type, source_ref, chunk_index, content, importance, created_at, metadata_json, embedding_json, expires_at
            FROM memory_embeddings
            WHERE user_id = ?
        """
        params: list[object] = [user_id]
        if source_types:
            placeholders = ", ".join("?" for _ in source_types)
            sql += f" AND source_type IN ({placeholders})"
            params.extend(source_types)
        sql, params = self._apply_embedding_source_ref_filter(
            sql=sql,
            params=params,
            source_ref_prefixes=source_ref_prefixes,
        )
        sql += " ORDER BY created_at DESC, id DESC"
        if safe_limit is not None:
            sql += " LIMIT ?"
            params.append(safe_limit)
        with self._session_factory() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            EmbeddingHit(
                id=int(row["id"]),
                user_id=row["user_id"],
                source_type=row["source_type"],
                source_ref=row["source_ref"],
                chunk_index=int(row["chunk_index"]),
                content=row["content"],
                importance=int(row["importance"]),
                similarity=0.0,
                created_at=row["created_at"],
                metadata=self._load_json_object(row["metadata_json"]),
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    def purge_expired_memory(self, *, now_iso: str | None = None) -> dict[str, int]:
        cutoff = now_iso or local_now_iso()
        with self._session_factory() as connection:
            expired_fact_rows = connection.execute(
                """
                SELECT id, user_id
                FROM memory_facts
                WHERE expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff,),
            ).fetchall()
            for row in expired_fact_rows:
                self._delete_fact_embedding(connection, user_id=row["user_id"], fact_id=int(row["id"]))
            facts_cursor = connection.execute(
                """
                DELETE FROM memory_facts
                WHERE expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff,),
            )
            embeddings_cursor = connection.execute(
                """
                DELETE FROM memory_embeddings
                WHERE expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff,),
            )
            connection.commit()
        return {
            "memory_facts": int(facts_cursor.rowcount),
            "memory_embeddings": int(embeddings_cursor.rowcount),
        }

    def add_turn(
        self,
        *,
        user_id: str,
        source: str,
        question: str,
        answer: str,
        question_created_at: str | None = None,
        answer_created_at: str | None = None,
        created_at: str | None = None,
        memory_processed: int = 0,
        turn_meta: dict[str, object] | None = None,
    ) -> int:
        question_created_at = question_created_at or local_now_iso()
        answer_created_at = answer_created_at or question_created_at
        created_at = created_at or answer_created_at
        with self._session_factory() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    user_id, source, question, answer,
                    question_created_at, answer_created_at, created_at, memory_processed, turn_meta_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source,
                    question,
                    answer,
                    question_created_at,
                    answer_created_at,
                    created_at,
                    memory_processed,
                    self._dump_json_object(turn_meta),
                ),
            )
            connection.commit()
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def add_turn_once(
        self,
        *,
        user_id: str,
        event_id: str,
        source: str,
        question: str,
        answer: str,
        question_created_at: str | None = None,
        answer_created_at: str | None = None,
        created_at: str | None = None,
        memory_processed: int = 0,
        turn_meta: dict[str, object] | None = None,
    ) -> int:
        """Insert an event once, using turn metadata so the schema stays compatible."""
        normalized_event_id = str(event_id).strip()
        if not normalized_event_id:
            raise ValueError("event_id must not be empty")
        question_created_at = question_created_at or local_now_iso()
        answer_created_at = answer_created_at or question_created_at
        created_at = created_at or answer_created_at
        metadata = dict(turn_meta or {})
        metadata["event_id"] = normalized_event_id
        with self._session_factory() as connection:
            # Serialize the lookup and insert across writers. Existing rows from
            # old deployments are still found by reading their JSON metadata.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id FROM conversation_turns
                WHERE user_id = ? AND json_extract(turn_meta_json, '$.event_id') = ?
                LIMIT 1
                """,
                (user_id, normalized_event_id),
            ).fetchone()
            if existing:
                connection.commit()
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO conversation_turns(
                    user_id, source, question, answer, question_created_at,
                    answer_created_at, created_at, memory_processed, turn_meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, source, question, answer, question_created_at, answer_created_at,
                 created_at, memory_processed, self._dump_json_object(metadata)),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def find_turn_by_event_id(self, *, user_id: str, event_id: str) -> ConversationTurnRecord | None:
        normalized_event_id = str(event_id).strip()
        if not normalized_event_id:
            return None
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json
                FROM conversation_turns
                WHERE user_id = ? AND json_extract(turn_meta_json, '$.event_id') = ?
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, normalized_event_id),
            ).fetchone()
        if row is None:
            return None
        return ConversationTurnRecord(
            id=int(row["id"]), user_id=row["user_id"], source=row["source"],
            question=row["question"], answer=row["answer"],
            question_created_at=row["question_created_at"],
            answer_created_at=row["answer_created_at"], created_at=row["created_at"],
            memory_processed=int(row["memory_processed"]),
            turn_meta=self._load_json_object(row["turn_meta_json"]),
        )

    def update_turn_metadata(self, *, user_id: str, turn_id: int, updates: dict[str, object]) -> bool:
        with self._session_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT turn_meta_json FROM conversation_turns WHERE user_id = ? AND id = ?",
                (user_id, int(turn_id)),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            metadata = self._load_json_object(row["turn_meta_json"])
            metadata.update(updates)
            cursor = connection.execute(
                "UPDATE conversation_turns SET turn_meta_json = ? WHERE user_id = ? AND id = ?",
                (self._dump_json_object(metadata), user_id, int(turn_id)),
            )
            connection.commit()
        return cursor.rowcount > 0

    def update_turn_result(
        self,
        *,
        user_id: str,
        turn_id: int,
        question: str,
        answer: str,
        updates: dict[str, object],
    ) -> bool:
        """Replace one persisted event result without changing its event identity."""
        now = local_now_iso()
        with self._session_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT turn_meta_json FROM conversation_turns WHERE user_id = ? AND id = ?",
                (user_id, int(turn_id)),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            previous = self._load_json_object(row["turn_meta_json"])
            metadata = dict(updates)
            if not metadata.get("event_id") and previous.get("event_id"):
                metadata["event_id"] = previous["event_id"]
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET question = ?, answer = ?, answer_created_at = ?, created_at = ?,
                    memory_processed = 0, turn_meta_json = ?, primary_topic = '',
                    primary_topic_embedding_json = '[]', question_embedding_json = '[]',
                    answer_embedding_json = '[]'
                WHERE user_id = ? AND id = ?
                """,
                (question, answer, now, now, self._dump_json_object(metadata), user_id, int(turn_id)),
            )
            connection.commit()
        return cursor.rowcount > 0

    def mark_turn_memory_processed(self, *, user_id: str, turn_id: int) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute(
                "UPDATE conversation_turns SET memory_processed = 1 WHERE user_id = ? AND id = ?",
                (user_id, int(turn_id)),
            )
            connection.commit()
        return cursor.rowcount > 0

    def set_turn_embeddings(
        self,
        *,
        user_id: str,
        turn_id: int,
        question_embedding: list[float],
        answer_embedding: list[float],
    ) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET question_embedding_json = ?, answer_embedding_json = ?, memory_processed = 1
                WHERE user_id = ? AND id = ?
                """,
                (
                    json.dumps([float(value) for value in question_embedding], ensure_ascii=False),
                    json.dumps([float(value) for value in answer_embedding], ensure_ascii=False),
                    user_id,
                    int(turn_id),
                ),
            )
            connection.commit()
        return cursor.rowcount > 0

    def set_turn_primary_topic(
        self,
        *,
        user_id: str,
        turn_id: int,
        primary_topic: str,
        primary_topic_embedding: list[float] | None = None,
    ) -> bool:
        with self._session_factory() as connection:
            if primary_topic_embedding is None:
                cursor = connection.execute(
                    "UPDATE conversation_turns SET primary_topic = ? WHERE user_id = ? AND id = ?",
                    (primary_topic.strip().lower(), user_id, int(turn_id)),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE conversation_turns
                    SET primary_topic = ?, primary_topic_embedding_json = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (
                        primary_topic.strip().lower(),
                        json.dumps([float(value) for value in primary_topic_embedding], ensure_ascii=False),
                        user_id,
                        int(turn_id),
                    ),
                )
            connection.commit()
        return cursor.rowcount > 0

    def search_turn_topic_similarities(self, topic_embedding: list[float], *, user_id: str) -> dict[int, float]:
        if not topic_embedding:
            return {}
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, primary_topic_embedding_json
                FROM conversation_turns
                WHERE user_id = ? AND primary_topic_embedding_json != '[]'
                """,
                (user_id,),
            ).fetchall()
        return {
            int(row["id"]): self._cosine_similarity(topic_embedding, vector)
            for row in rows
            if (vector := self._load_embedding_vector(row["primary_topic_embedding_json"]))
            and len(vector) == len(topic_embedding)
        }

    def get_turns_by_ids(self, *, user_id: str, turn_ids: list[int]) -> list[ConversationTurnRecord]:
        ids = sorted({int(turn_id) for turn_id in turn_ids if int(turn_id) > 0})
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json, primary_topic
                FROM conversation_turns
                WHERE user_id = ? AND id IN ("""
                + placeholders
                + ")",
                [user_id, *ids],
            ).fetchall()
        return [
            ConversationTurnRecord(
                id=int(row["id"]),
                user_id=row["user_id"],
                source=row["source"],
                question=row["question"],
                answer=row["answer"],
                question_created_at=row["question_created_at"],
                answer_created_at=row["answer_created_at"],
                created_at=row["created_at"],
                memory_processed=int(row["memory_processed"]),
                turn_meta=self._load_json_object(row["turn_meta_json"]),
                primary_topic=str(row["primary_topic"] or ""),
            )
            for row in rows
        ]

    def search_turns_by_exact_text(
        self,
        *,
        user_id: str,
        phrases: list[str],
        limit: int = 50,
    ) -> list[ConversationTurnRecord]:
        normalized_phrases = sorted({phrase.strip().casefold() for phrase in phrases if len(phrase.strip()) >= 3})
        if not normalized_phrases:
            return []
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json, primary_topic
                FROM conversation_turns
                WHERE user_id = ?
                ORDER BY id DESC
                """,
                (user_id,),
            ).fetchall()
        matches: list[ConversationTurnRecord] = []
        for row in rows:
            # Retrieval phrases must match the actual dialogue, not derived metadata.
            haystack = f"{row['question']}\n{row['answer']}".casefold()
            if not any(phrase in haystack for phrase in normalized_phrases):
                continue
            matches.append(
                ConversationTurnRecord(
                    id=int(row["id"]),
                    user_id=row["user_id"],
                    source=row["source"],
                    question=row["question"],
                    answer=row["answer"],
                    question_created_at=row["question_created_at"],
                    answer_created_at=row["answer_created_at"],
                    created_at=row["created_at"],
                    memory_processed=int(row["memory_processed"]),
                    turn_meta=self._load_json_object(row["turn_meta_json"]),
                    primary_topic=str(row["primary_topic"] or ""),
                )
            )
            if len(matches) >= max(1, min(int(limit), 100)):
                break
        return matches

    def search_turn_embeddings(
        self,
        query_embedding: list[float],
        *,
        user_id: str,
        primary_topic: str = "",
    ) -> list[EmbeddingHit]:
        if not query_embedding:
            return []
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, question, answer, created_at, turn_meta_json, primary_topic,
                       question_embedding_json, answer_embedding_json
                FROM conversation_turns
                WHERE user_id = ? AND (? = '' OR primary_topic = ?)
                """,
                (user_id, primary_topic.strip().lower(), primary_topic.strip().lower()),
            ).fetchall()
        hits: list[EmbeddingHit] = []
        for row in rows:
            scores = [
                self._cosine_similarity(query_embedding, vector)
                for vector in (
                    self._load_embedding_vector(row["question_embedding_json"]),
                    self._load_embedding_vector(row["answer_embedding_json"]),
                )
                if vector and len(vector) == len(query_embedding)
            ]
            if not scores:
                continue
            hits.append(
                EmbeddingHit(
                    id=int(row["id"]),
                    user_id=row["user_id"],
                    source_type="dialogue_turn",
                    source_ref=f"dialogue:turn:{row['id']}",
                    chunk_index=0,
                    content=f"Question:\n{row['question']}\n\nAnswer:\n{row['answer']}",
                    importance=5,
                    similarity=max(scores),
                    created_at=row["created_at"],
                    metadata={
                        "turn_meta": self._load_json_object(row["turn_meta_json"]),
                        "primary_topic": str(row["primary_topic"] or ""),
                    },
                )
            )
        hits.sort(key=lambda item: (item.similarity, item.id), reverse=True)
        return hits

    def add_fact(
        self,
        *,
        user_id: str,
        category: str,
        subject: str,
        predicate: str,
        value: str,
        confidence: float,
        importance: int,
        source_ref: str,
        tags: list[str] | None = None,
        expires_at: datetime | None = None,
        inclusion: str | None = None,
        fact_id: int | None = None,
    ) -> int:
        inclusion_mode = self._normalize_fact_inclusion(inclusion)
        if fact_id is not None and (
            isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id <= 0
        ):
            raise ValueError("fact_id must be a positive integer")
        now = local_now_iso()
        expires_at_iso = expires_at.isoformat() if expires_at else None
        with self._session_factory() as connection:
            if fact_id is not None:
                existing = connection.execute(
                    """
                    SELECT id, user_id, category, subject, predicate, value, confidence, importance,
                           source_ref, last_seen, expires_at, tags_json
                    FROM memory_facts
                    WHERE user_id = ? AND id = ?
                    """,
                    (user_id, int(fact_id)),
                ).fetchone()
                if existing is None:
                    raise ValueError(f"Fact {int(fact_id)} does not exist for user {user_id!r}")
                if (existing["category"], existing["subject"], existing["predicate"]) != (
                    category,
                    subject,
                    predicate,
                ):
                    raise ValueError("fact_id target does not match category, subject, and predicate")
            else:
                existing = connection.execute(
                    """
                    SELECT id, user_id, category, subject, predicate, value, confidence, importance,
                           source_ref, last_seen, expires_at, tags_json
                    FROM memory_facts
                    WHERE user_id = ? AND category = ? AND subject = ? AND predicate = ?
                    ORDER BY importance DESC, last_seen DESC, id DESC
                    LIMIT 1
                    """,
                    (user_id, category, subject, predicate),
                ).fetchone()
            if existing:
                merged_tags = sorted(set(self._load_fact_tags(existing["tags_json"]) + self._normalize_fact_tags(tags)))
                merged_tags = self._apply_fact_inclusion(merged_tags, inclusion_mode)
                next_value = value if value and value != existing["value"] else existing["value"]
                old_fact = self._row_to_fact_dict(existing)
                next_fact = {
                    **old_fact,
                    "category": category,
                    "subject": subject,
                    "predicate": predicate,
                    "value": next_value,
                    "expires_at": expires_at_iso,
                    "tags": merged_tags,
                }
                connection.execute(
                    """
                    UPDATE memory_facts
                    SET value = ?, confidence = ?, importance = ?, last_seen = ?, tags_json = ?, expires_at = ?, source_ref = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (
                        next_value,
                        max(existing["confidence"], confidence),
                        max(existing["importance"], importance),
                        now,
                        json.dumps(merged_tags),
                        expires_at_iso,
                        source_ref,
                        user_id,
                        existing["id"],
                    ),
                )
                if self._fact_embedding_needs_invalidation(old_fact, next_fact):
                    self._delete_fact_embedding(connection, user_id=user_id, fact_id=int(existing["id"]))
                connection.commit()
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO memory_facts(user_id, category, subject, predicate, value, confidence, importance, source_ref, last_seen, expires_at, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    category,
                    subject,
                    predicate,
                    value,
                    confidence,
                    importance,
                    source_ref,
                    now,
                    expires_at_iso,
                    json.dumps(self._apply_fact_inclusion(self._normalize_fact_tags(tags), inclusion_mode)),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def upsert_news_dislike(
        self,
        *,
        user_id: str,
        subject: str,
        value: str,
        confidence: float,
        importance: int,
        source_ref: str,
    ) -> int:
        """Persist one permanent news-topic opt-out and collapse legacy rows."""
        now = local_now_iso()
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM memory_facts
                WHERE user_id = ?
                  AND category = 'news_context'
                  AND subject = ?
                  AND predicate IN ('dislikes', 'likes')
                ORDER BY CASE predicate WHEN 'dislikes' THEN 0 ELSE 1 END,
                         importance DESC, last_seen DESC, id DESC
                """,
                (user_id, subject),
            ).fetchall()
            if rows:
                kept_id = int(rows[0]["id"])
                for row in rows:
                    self._delete_fact_embedding(connection, user_id=user_id, fact_id=int(row["id"]))
                connection.execute(
                    """
                    UPDATE memory_facts
                    SET predicate = 'dislikes', value = ?, confidence = ?, importance = ?,
                        source_ref = ?, last_seen = ?, expires_at = NULL, tags_json = ?
                    WHERE id = ?
                    """,
                    (value, confidence, importance, source_ref, now, json.dumps(["news_filter"]), kept_id),
                )
                redundant_ids = [int(row["id"]) for row in rows[1:]]
                if redundant_ids:
                    placeholders = ", ".join("?" for _ in redundant_ids)
                    connection.execute(f"DELETE FROM memory_facts WHERE id IN ({placeholders})", redundant_ids)
                connection.commit()
                return kept_id

            cursor = connection.execute(
                """
                INSERT INTO memory_facts(
                    user_id, category, subject, predicate, value, confidence, importance,
                    source_ref, last_seen, expires_at, tags_json
                )
                VALUES (?, 'news_context', ?, 'dislikes', ?, ?, ?, ?, ?, NULL, ?)
                """,
                (user_id, subject, value, confidence, importance, source_ref, now, json.dumps(["news_filter"])),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_active_news_dislikes(self, *, user_id: str, limit: int = 100) -> list[dict]:
        """Return only this user's non-expired canonical news topic opt-outs."""
        safe_limit = max(1, min(int(limit), 500))
        now = local_now_iso()
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, category, subject, predicate, value, confidence, importance,
                       source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                WHERE user_id = ?
                  AND category = 'news_context'
                  AND subject LIKE 'topic:%'
                  AND predicate = 'dislikes'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY importance DESC, last_seen DESC, id DESC
                LIMIT ?
                """,
                (user_id, now, safe_limit),
            ).fetchall()
        return [self._row_to_fact_dict(row) for row in rows]

    def delete_news_topic_facts(self, *, user_id: str, subject: str) -> int:
        """Remove all current and legacy preference rows for one news topic."""
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM memory_facts
                WHERE user_id = ?
                  AND category = 'news_context'
                  AND subject = ?
                  AND predicate IN ('dislikes', 'likes')
                """,
                (user_id, subject),
            ).fetchall()
            for row in rows:
                self._delete_fact_embedding(connection, user_id=user_id, fact_id=int(row["id"]))
            cursor = connection.execute(
                """
                DELETE FROM memory_facts
                WHERE user_id = ?
                  AND category = 'news_context'
                  AND subject = ?
                  AND predicate IN ('dislikes', 'likes')
                """,
                (user_id, subject),
            )
            connection.commit()
            return int(cursor.rowcount)

    def memory_counts(self, user_id: str) -> dict[str, int]:
        """Count administrative memory groups without loading their contents."""
        with self._session_factory() as connection:
            facts = connection.execute(
                "SELECT count(*) FROM memory_facts WHERE user_id IN (?, 'global')", (user_id,)
            ).fetchone()[0]
            turns = connection.execute(
                "SELECT count(*) FROM conversation_turns WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            knowledge = connection.execute(
                """SELECT count(*) FROM memory_embeddings
                   WHERE user_id IN (?, 'global')
                     AND source_type IN ('knowledge_text_personal', 'knowledge_text_global')""",
                (user_id,),
            ).fetchone()[0]
        return {"facts": int(facts), "turns": int(turns), "knowledge": int(knowledge)}

    def list_facts(self, user_id: str, limit: int = 100, *, include_global: bool = True) -> list[dict]:
        safe_limit = max(1, min(limit, 500))
        scope_clause = "WHERE user_id in ( ?,'global')" if include_global else "WHERE user_id = ?"
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, category, subject, predicate, value, confidence, importance, source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                """
                + scope_clause
                + """
                ORDER BY importance DESC, last_seen DESC, id DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return [self._row_to_fact_dict(row) for row in rows]

    def list_fact_page(
        self,
        user_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        """List one user-scoped fact page using an id keyset cursor."""
        safe_after_id = max(0, int(after_id))
        safe_limit = max(1, min(int(limit), 1000))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, category, subject, predicate, value, confidence, importance,
                       source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                WHERE user_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (user_id, safe_after_id, safe_limit),
            ).fetchall()
        return [self._row_to_fact_dict(row) for row in rows]

    def get_fact(self, user_id: str, fact_id: int) -> dict | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, category, subject, predicate, value, confidence, importance, source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                WHERE user_id = ? AND id = ?
                """,
                (user_id, fact_id),
            ).fetchone()
        return self._row_to_fact_dict(row) if row else None

    def update_fact(
        self,
        fact_id: int,
        *,
        user_id: str,
        category: str,
        subject: str,
        predicate: str,
        value: str,
        confidence: float,
        importance: int,
        source_ref: str,
        tags: list[str] | None = None,
        expires_at: datetime | None = None,
        inclusion: str | None = None,
        expected_revision: str | None = None,
    ) -> dict | None:
        inclusion_mode = self._normalize_fact_inclusion(inclusion)
        now = local_now_iso()
        expires_at_iso = expires_at.isoformat() if expires_at else None
        with self._session_factory() as connection:
            if expected_revision is not None:
                # Hold the write reservation through version check and mutation;
                # a second writer cannot slip between those two operations.
                connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, user_id, category, subject, predicate, value, confidence, importance,
                       source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                WHERE user_id = ? AND id = ?
                """,
                (user_id, int(fact_id)),
            ).fetchone()
            if existing is None:
                return None
            old_fact = self._row_to_fact_dict(existing)
            if expected_revision is not None and fact_revision(old_fact) != expected_revision:
                raise ValueError("Fact changed since it was loaded")
            next_tags = self._normalize_fact_tags(tags)
            if inclusion_mode is not None:
                next_tags = self._apply_fact_inclusion(
                    sorted(set(self._load_fact_tags(existing["tags_json"]) + next_tags)),
                    inclusion_mode,
                )
            next_fact = {
                **old_fact,
                "category": category,
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "expires_at": expires_at_iso,
                "tags": next_tags,
            }
            cursor = connection.execute(
                """
                UPDATE memory_facts
                SET category = ?, subject = ?, predicate = ?, value = ?, confidence = ?, importance = ?, source_ref = ?, last_seen = ?, expires_at = ?, tags_json = ?
                WHERE user_id = ? AND id = ?
                """,
                (
                    category,
                    subject,
                    predicate,
                    value,
                    confidence,
                    importance,
                    source_ref,
                    now,
                    expires_at_iso,
                    json.dumps(next_tags),
                    user_id,
                    fact_id,
                ),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                return None
            if self._fact_embedding_needs_invalidation(old_fact, next_fact):
                self._delete_fact_embedding(connection, user_id=user_id, fact_id=int(fact_id))
            connection.commit()
        return self.get_fact(user_id, fact_id)

    def delete_fact(self, user_id: str, fact_id: int, *, expected_revision: str | None = None) -> bool:
        with self._session_factory() as connection:
            if expected_revision is not None:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT id, user_id, category, subject, predicate, value, confidence,
                           importance, source_ref, last_seen, expires_at, tags_json
                    FROM memory_facts WHERE user_id = ? AND id = ?
                    """,
                    (user_id, int(fact_id)),
                ).fetchone()
                if existing is None:
                    return False
                if fact_revision(self._row_to_fact_dict(existing)) != expected_revision:
                    raise ValueError("Fact changed since it was loaded")
            self._delete_fact_embedding(connection, user_id=user_id, fact_id=int(fact_id))
            cursor = connection.execute("DELETE FROM memory_facts WHERE user_id = ? AND id = ?", (user_id, fact_id))
            connection.commit()
            return cursor.rowcount > 0

    def delete_fact_ids(self, *, user_id: str, fact_ids: list[int]) -> int:
        ids = [int(item) for item in fact_ids if int(item) > 0]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._session_factory() as connection:
            for fact_id in ids:
                self._delete_fact_embedding(connection, user_id=user_id, fact_id=fact_id)
            cursor = connection.execute(
                f"DELETE FROM memory_facts WHERE user_id = ? AND id IN ({placeholders})",
                [user_id, *ids],
            )
            connection.commit()
            return int(cursor.rowcount)

    def search_facts_by_text(self, *, user_id: str, query: str, limit: int = 50) -> list[dict]:
        terms = [term.casefold() for term in query.split() if len(term.strip()) >= 3]
        if not terms:
            terms = [query.casefold().strip()] if query.strip() else []
        if not terms:
            return []
        facts = self.list_facts(user_id, limit=500, include_global=False)
        matches: list[dict] = []
        for fact in facts:
            haystack = " ".join(
                str(fact.get(field, ""))
                for field in ("category", "subject", "predicate", "value", "source_ref")
            ).casefold()
            if any(term in haystack for term in terms):
                matches.append(fact)
                if len(matches) >= limit:
                    break
        return matches


    def migrate_user_id_values(self, *, old_user_ids: list[str], new_user_id: str) -> dict[str, int]:
        normalized_old_user_ids = [item.strip() for item in old_user_ids if item and item.strip()]
        normalized_new_user_id = new_user_id.strip()
        if not normalized_old_user_ids or not normalized_new_user_id:
            return {}
        tables = [
            "conversation_turns",
            "memory_facts",
            "memory_embeddings",
            "knowledge_upload_sessions",
        ]
        placeholders = ", ".join("?" for _ in normalized_old_user_ids)
        counts: dict[str, int] = {}
        with self._session_factory() as connection:
            for table_name in tables:
                cursor = connection.execute(
                    f"UPDATE {table_name} SET user_id = ? WHERE user_id IN ({placeholders})",
                    [normalized_new_user_id, *normalized_old_user_ids],
                )
                counts[table_name] = int(cursor.rowcount)
            connection.commit()
        return counts

    def recent_turns(
        self,
        user_id: str,
        limit: int = 10,
        hours: int | None = None,
        chat_id: str | None = None,
    ) -> list[ConversationTurnRecord]:
        params: list[object] = [user_id]
        cutoff_clause = ""
        if hours is not None:
            cutoff_clause = " AND created_at >= ?"
            params.append((datetime.now().astimezone() - timedelta(hours=hours)).isoformat())
        if chat_id is not None:
            chat_id_expression = (
                "COALESCE(NULLIF(json_extract(turn_meta_json, '$.chat_id'), ''), "
                "NULLIF(json_extract(turn_meta_json, '$.callback_metadata.chat_id'), ''), "
                "NULLIF(json_extract(turn_meta_json, '$.callback_metadata.target_chat_id'), ''))"
            )
            cutoff_clause += f" AND ({chat_id_expression} IS NULL OR {chat_id_expression} = ?)"
            params.append(str(chat_id).strip())
        params.append(limit)
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json
                FROM conversation_turns
                WHERE user_id = ?
                """
                + cutoff_clause
                + """
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            ConversationTurnRecord(
                id=int(row["id"]),
                user_id=row["user_id"],
                source=row["source"],
                question=row["question"],
                answer=row["answer"],
                question_created_at=row["question_created_at"],
                answer_created_at=row["answer_created_at"],
                created_at=row["created_at"],
                memory_processed=int(row["memory_processed"]),
                turn_meta=self._load_json_object(row["turn_meta_json"]),
            )
            for row in reversed(rows)
        ]

    def get_turn(self, *, user_id: str, turn_id: int) -> ConversationTurnRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json, primary_topic
                FROM conversation_turns
                WHERE user_id = ? AND id = ?
                """,
                (user_id, int(turn_id)),
            ).fetchone()
        if row is None:
            return None
        return ConversationTurnRecord(
            id=int(row["id"]),
            user_id=row["user_id"],
            source=row["source"],
            question=row["question"],
            answer=row["answer"],
            question_created_at=row["question_created_at"],
            answer_created_at=row["answer_created_at"],
            created_at=row["created_at"],
            memory_processed=int(row["memory_processed"]),
            turn_meta=self._load_json_object(row["turn_meta_json"]),
            primary_topic=str(row["primary_topic"] or ""),
        )

    def list_turns(self, *, user_id: str, limit: int = 500) -> list[ConversationTurnRecord]:
        safe_limit = max(1, min(int(limit), 2000))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json, primary_topic
                FROM conversation_turns
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return [
            ConversationTurnRecord(
                id=int(row["id"]),
                user_id=row["user_id"],
                source=row["source"],
                question=row["question"],
                answer=row["answer"],
                question_created_at=row["question_created_at"],
                answer_created_at=row["answer_created_at"],
                created_at=row["created_at"],
                memory_processed=int(row["memory_processed"]),
                turn_meta=self._load_json_object(row["turn_meta_json"]),
                primary_topic=str(row["primary_topic"] or ""),
            )
            for row in rows
        ]

    def turn_ids_missing_primary_topic_index(
        self,
        *,
        user_id: str,
        date: str | None = None,
        limit: int | None = None,
    ) -> list[int]:
        params: list[object] = [user_id]
        date_clause = ""
        if date:
            date_clause = " AND created_at LIKE ?"
            params.append(f"{date}%")
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM conversation_turns
                WHERE user_id = ?
                  AND (TRIM(primary_topic) = '' OR primary_topic_embedding_json = '[]')
                """
                + date_clause
                + " ORDER BY id ASC"
                + limit_clause,
                params,
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def old_turns(self, hours: int = 24, limit: int = 500) -> list[ConversationTurnRecord]:
        cutoff = (datetime.now().astimezone() - timedelta(hours=hours)).isoformat()
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json
                FROM conversation_turns
                WHERE created_at < ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (cutoff, max(1, limit)),
            ).fetchall()
        return [
            ConversationTurnRecord(
                id=int(row["id"]),
                user_id=row["user_id"],
                source=row["source"],
                question=row["question"],
                answer=row["answer"],
                question_created_at=row["question_created_at"],
                answer_created_at=row["answer_created_at"],
                created_at=row["created_at"],
                memory_processed=int(row["memory_processed"]),
                turn_meta=self._load_json_object(row["turn_meta_json"]),
            )
            for row in rows
        ]

    def unprocessed_turns(self, *, user_id: str, limit: int = 5) -> list[ConversationTurnRecord]:
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json, primary_topic
                FROM conversation_turns
                WHERE user_id = ? AND memory_processed = 0
                ORDER BY id ASC
                LIMIT ?
                """,
                (user_id, max(1, limit)),
            ).fetchall()
        return [
            ConversationTurnRecord(
                id=int(row["id"]),
                user_id=row["user_id"],
                source=row["source"],
                question=row["question"],
                answer=row["answer"],
                question_created_at=row["question_created_at"],
                answer_created_at=row["answer_created_at"],
                created_at=row["created_at"],
            memory_processed=int(row["memory_processed"]),
            turn_meta=self._load_json_object(row["turn_meta_json"]),
            primary_topic=str(row["primary_topic"] or ""),
            )
            for row in rows
        ]

    def delete_turn(self, turn_id: int) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute("DELETE FROM conversation_turns WHERE id = ?", (turn_id,))
            connection.commit()
            return cursor.rowcount > 0

    def delete_turn_ids(self, *, user_id: str, turn_ids: list[int]) -> int:
        ids = [int(item) for item in turn_ids if int(item) > 0]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._session_factory() as connection:
            cursor = connection.execute(
                f"DELETE FROM conversation_turns WHERE user_id = ? AND id IN ({placeholders})",
                [user_id, *ids],
            )
            connection.commit()
            return int(cursor.rowcount)

    def search_turns_by_text(self, *, user_id: str, query: str, limit: int = 50) -> list[ConversationTurnRecord]:
        terms = [term.casefold() for term in query.split() if len(term.strip()) >= 3]
        if not terms:
            terms = [query.casefold().strip()] if query.strip() else []
        if not terms:
            return []
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed, turn_meta_json, primary_topic
                FROM conversation_turns
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 500
                """,
                (user_id,),
            ).fetchall()
        matches: list[ConversationTurnRecord] = []
        for row in rows:
            haystack = f"{row['question']} {row['answer']} {row['source']} {row['turn_meta_json']}".casefold()
            if any(term in haystack for term in terms):
                matches.append(
                    ConversationTurnRecord(
                        id=int(row["id"]),
                        user_id=row["user_id"],
                        source=row["source"],
                        question=row["question"],
                        answer=row["answer"],
                        question_created_at=row["question_created_at"],
                        answer_created_at=row["answer_created_at"],
                        created_at=row["created_at"],
                memory_processed=int(row["memory_processed"]),
                turn_meta=self._load_json_object(row["turn_meta_json"]),
                primary_topic=str(row["primary_topic"] or ""),
                    )
                )
                if len(matches) >= limit:
                    break
        return matches


    def add_knowledge_upload_session(
        self,
        *,
        user_id: str,
        source_ref: str,
        file_name: str,
        file_mime_type: str | None = None,
        file_size: int | None = None,
        file_id: str | None = None,
        downloaded_file_path: str | None = None,
        downloaded_file_url: str | None = None,
        status: str = "pending_scope",
        created_at: str | None = None,
        resolved_at: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> int:
        created_at = created_at or local_now_iso()
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO knowledge_upload_sessions(
                    user_id, source_ref, file_name, file_mime_type, file_size, file_id,
                    downloaded_file_path, downloaded_file_url, status, created_at, resolved_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source_ref,
                    file_name,
                    file_mime_type,
                    file_size,
                    file_id,
                    downloaded_file_path,
                    downloaded_file_url,
                    status,
                    created_at,
                    resolved_at,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_knowledge_upload_session(self, session_id: int, *, user_id: str) -> KnowledgeUploadSessionRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source_ref, file_name, file_mime_type, file_size, file_id,
                       downloaded_file_path, downloaded_file_url, status, created_at, resolved_at, metadata_json
                FROM knowledge_upload_sessions
                WHERE user_id = ? AND id = ?
                """,
                (user_id, session_id),
            ).fetchone()
        return self._row_to_knowledge_upload_session(row) if row else None

    def set_knowledge_upload_session_status(
        self,
        session_id: int,
        *,
        user_id: str,
        status: str,
        resolved_at: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False) if metadata is not None else None
        with self._session_factory() as connection:
            if metadata_json is None:
                cursor = connection.execute(
                    """
                    UPDATE knowledge_upload_sessions
                    SET status = ?, resolved_at = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, resolved_at, user_id, session_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE knowledge_upload_sessions
                    SET status = ?, resolved_at = ?, metadata_json = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, resolved_at, metadata_json, user_id, session_id),
                )
            connection.commit()
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT id, user_id, source_ref, file_name, file_mime_type, file_size, file_id,
                       downloaded_file_path, downloaded_file_url, status, created_at, resolved_at, metadata_json
                FROM knowledge_upload_sessions
                WHERE user_id = ? AND id = ?
                """,
                (user_id, session_id),
            ).fetchone()
        return self._row_to_knowledge_upload_session_dict(row) if row else None

    def search_embeddings(
        self,
        query_embedding: list[float],
        user_id: str,
        limit: int = 5,
        source_types: list[str] | None = None,
        source_ref_prefixes: list[str] | None = None,
        *,
        include_global: bool = True,
    ) -> list[EmbeddingHit]:
        if not query_embedding:
            return []

        effective_user_id = (user_id or "").strip()
        if not effective_user_id:
            raise ValueError("Embedding search requires a non-empty user_id")
        global_user_id = "global"
        if include_global and effective_user_id != global_user_id:
            sql = """
                SELECT id, user_id, source_type, source_ref, chunk_index, content, importance, created_at, metadata_json, embedding_json, expires_at
                FROM memory_embeddings
                WHERE user_id IN (?, ?)
            """
            params = [effective_user_id, global_user_id]
        else:
            sql = """
                SELECT id, user_id, source_type, source_ref, chunk_index, content, importance, created_at, metadata_json, embedding_json, expires_at
                FROM memory_embeddings
                WHERE user_id = ?
            """
            params = [effective_user_id]
        if source_types:
            placeholders = ", ".join("?" for _ in source_types)
            sql += f" AND source_type IN ({placeholders})"
            params.extend(source_types)
        sql, params = self._apply_embedding_source_ref_filter(
            sql=sql,
            params=params,
            source_ref_prefixes=source_ref_prefixes,
        )
        if include_global and effective_user_id != global_user_id:
            sql += " ORDER BY CASE WHEN user_id = ? THEN 0 ELSE 1 END, importance DESC, created_at DESC, id DESC"
            params.append(effective_user_id)
        else:
            sql += " ORDER BY importance DESC, created_at DESC, id DESC"

        with self._session_factory() as connection:
            rows = connection.execute(sql, params).fetchall()

        scored: list[EmbeddingHit] = []
        for row in rows:
            stored_embedding = self._load_embedding_vector(row["embedding_json"])
            if not stored_embedding or len(stored_embedding) != len(query_embedding):
                continue
            similarity = self._cosine_similarity(query_embedding, stored_embedding)
            scored.append(
                EmbeddingHit(
                    id=int(row["id"]),
                    user_id=row["user_id"],
                    source_type=row["source_type"],
                    source_ref=row["source_ref"],
                    chunk_index=int(row["chunk_index"]),
                    content=row["content"],
                    importance=int(row["importance"]),
                    similarity=similarity,
                    created_at=row["created_at"],
                    metadata=self._load_json_object(row["metadata_json"]),
                    expires_at=row["expires_at"],
                )
            )

        scored.sort(key=lambda item: (item.similarity, item.importance, item.id), reverse=True)
        return scored if limit is None else scored[: max(1, limit)]

    @staticmethod
    def _apply_embedding_source_ref_filter(
        *,
        sql: str,
        params: list[object],
        source_ref_prefixes: list[str] | None,
    ) -> tuple[str, list[object]]:
        prefixes = [str(item).strip() for item in (source_ref_prefixes or []) if str(item).strip()]
        if not prefixes:
            return sql, params
        clauses: list[str] = []
        expanded_params: list[object] = []
        for prefix in prefixes:
            clauses.append("source_ref LIKE ?")
            expanded_params.append(f"{prefix}%")
            if prefix == "dialogue:turn:":
                clauses.append("source_ref LIKE ?")
                expanded_params.append("turn:%")
        sql += " AND (" + " OR ".join(clauses) + ")"
        params.extend(expanded_params)
        return sql, params

    @staticmethod
    def _normalize_fact_inclusion(inclusion: str | None) -> str | None:
        if inclusion is None:
            return None
        normalized = str(inclusion).strip().casefold()
        if normalized not in {"mandatory", "contextual"}:
            raise ValueError("inclusion must be 'mandatory' or 'contextual'")
        return normalized

    @staticmethod
    def _normalize_fact_tags(tags: list[str] | None) -> list[str]:
        if not isinstance(tags, list):
            return []
        return [str(tag).strip() for tag in tags if str(tag).strip()]

    @staticmethod
    def _load_fact_tags(raw_json: str | None) -> list[str]:
        try:
            value = json.loads(raw_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(tag).strip() for tag in value if str(tag).strip()] if isinstance(value, list) else []

    @staticmethod
    def _apply_fact_inclusion(tags: list[str], inclusion: str | None) -> list[str]:
        result = sorted(set(tags))
        if inclusion == "mandatory":
            if MANDATORY_TAG not in result:
                result.append(MANDATORY_TAG)
                result.sort()
        elif inclusion == "contextual":
            result = [tag for tag in result if tag != MANDATORY_TAG]
        return result

    @staticmethod
    def _delete_fact_embedding(connection, *, user_id: str, fact_id: int) -> None:
        connection.execute(
            """
            DELETE FROM memory_embeddings
            WHERE user_id = ? AND source_type = ? AND source_ref = ?
            """,
            (user_id, FACT_EMBEDDING_SOURCE_TYPE, f"{FACT_EMBEDDING_SOURCE_PREFIX}{int(fact_id)}"),
        )

    @staticmethod
    def _fact_embedding_needs_invalidation(old_fact: dict, next_fact: dict) -> bool:
        return fact_embedding_text(old_fact) != fact_embedding_text(next_fact) or (
            not is_mandatory_fact(old_fact) and is_mandatory_fact(next_fact)
        )

    def _row_to_fact_dict(self, row) -> dict:
        return {
            "id": int(row["id"]),
            "user_id": row["user_id"],
            "category": row["category"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "value": row["value"],
            "confidence": float(row["confidence"]),
            "importance": int(row["importance"]),
            "source_ref": row["source_ref"],
            "last_seen": row["last_seen"],
            "expires_at": row["expires_at"],
            "tags": json.loads(row["tags_json"] or "[]"),
        }


    def _row_to_knowledge_upload_session(self, row) -> KnowledgeUploadSessionRecord:
        return KnowledgeUploadSessionRecord(
            id=int(row["id"]),
            user_id=row["user_id"],
            source_ref=row["source_ref"],
            file_name=row["file_name"],
            file_mime_type=row["file_mime_type"],
            file_size=int(row["file_size"]) if row["file_size"] is not None else None,
            file_id=row["file_id"],
            downloaded_file_path=row["downloaded_file_path"],
            downloaded_file_url=row["downloaded_file_url"],
            status=row["status"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            metadata=self._load_json_object(row["metadata_json"]),
        )


    def _row_to_knowledge_upload_session_dict(self, row) -> dict[str, object]:
        session = self._row_to_knowledge_upload_session(row)
        return session.model_dump()

    @staticmethod
    def _load_embedding_vector(raw_json: str) -> list[float]:
        try:
            values = json.loads(raw_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(values, list):
            return []
        vector: list[float] = []
        for value in values:
            try:
                vector.append(float(value))
            except (TypeError, ValueError):
                return []
        return vector

    @staticmethod
    def _load_json_object(raw_json: str) -> dict[str, object]:
        try:
            value = json.loads(raw_json or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _dump_json_object(value: dict[str, object] | None) -> str:
        return json.dumps(value or {}, ensure_ascii=False)


    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return dot / (left_norm * right_norm)

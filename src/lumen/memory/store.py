from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone

from lumen.models import ConversationTurnRecord, EmbeddingHit, MemoryHit


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
        embedding_model: str,
        chunk_index: int = 0,
        role: str | None = None,
        importance: int = 5,
        metadata: dict[str, object] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        embedding_json = json.dumps([float(value) for value in embedding], ensure_ascii=False)

        with self._session_factory() as connection:
            existing = connection.execute(
                """
                SELECT id, content_hash
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
                    SET role = ?, content = ?, content_hash = ?, embedding_model = ?, embedding_dimensions = ?,
                        embedding_json = ?, importance = ?, metadata_json = ?, updated_at = ?, last_seen = ?
                    WHERE id = ?
                    """,
                    (
                        role,
                        content,
                        content_hash,
                        embedding_model,
                        len(embedding),
                        embedding_json,
                        importance,
                        metadata_json,
                        now,
                        now,
                        existing["id"],
                    ),
                )
                connection.commit()
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO memory_embeddings(
                    user_id, source_type, source_ref, chunk_index, role, content, content_hash, embedding_model,
                    embedding_dimensions, embedding_json, importance, metadata_json, created_at, updated_at, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source_type,
                    source_ref,
                    chunk_index,
                    role,
                    content,
                    content_hash,
                    embedding_model,
                    len(embedding),
                    embedding_json,
                    importance,
                    metadata_json,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def add_turn(
        self,
        *,
        conversation_id: str,
        user_id: str,
        source: str,
        question: str,
        answer: str,
        question_created_at: str | None = None,
        answer_created_at: str | None = None,
        created_at: str | None = None,
        memory_processed: int = 0,
    ) -> int:
        question_created_at = question_created_at or datetime.now(timezone.utc).isoformat()
        answer_created_at = answer_created_at or question_created_at
        created_at = created_at or answer_created_at
        with self._session_factory() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    conversation_id, user_id, source, question, answer,
                    question_created_at, answer_created_at, created_at, memory_processed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    user_id,
                    source,
                    question,
                    answer,
                    question_created_at,
                    answer_created_at,
                    created_at,
                    memory_processed,
                ),
            )
            connection.commit()
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

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
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._session_factory() as connection:
            existing = connection.execute(
                """
                SELECT id, category, subject, predicate, value, confidence, importance, tags_json, source_ref, expires_at
                FROM memory_facts
                WHERE user_id = ? AND category = ? AND subject = ? AND predicate = ?
                ORDER BY importance DESC, last_seen DESC, id DESC
                LIMIT 1
                """,
                (user_id, category, subject, predicate),
            ).fetchone()
            if existing:
                merged_tags = sorted(set(json.loads(existing["tags_json"] or "[]") + (tags or [])))
                next_value = value if value and value != existing["value"] else existing["value"]
                connection.execute(
                    """
                UPDATE memory_facts
                SET value = ?, confidence = ?, importance = ?, last_seen = ?, tags_json = ?, expires_at = ?, source_ref = ?
                WHERE id = ?
                    """,
                    (
                        next_value,
                        max(existing["confidence"], confidence),
                        max(existing["importance"], importance),
                        now,
                        json.dumps(merged_tags),
                        expires_at.isoformat() if expires_at else None,
                        source_ref,
                        existing["id"],
                    ),
                )
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
                    expires_at.isoformat() if expires_at else None,
                    json.dumps(tags or []),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def search(self, query: str, user_id: str, limit: int = 10) -> list[MemoryHit]:
        tokens = [token.strip().lower() for token in re.split(r"\W+", query) if token.strip()]
        has_non_ascii = any(any(ord(char) > 127 for char in token) for token in tokens)
        sql = """
            SELECT id, user_id, category, subject, predicate, value, confidence, importance, source_ref
            FROM memory_facts
            WHERE user_id = ?
        """
        params: list[str] = [user_id]
        if tokens and not has_non_ascii:
            conditions = []
            for token in tokens:
                pattern = f"%{token}%"
                conditions.extend(
                    [
                        "LOWER(category) LIKE ?",
                        "LOWER(subject) LIKE ?",
                        "LOWER(predicate) LIKE ?",
                        "LOWER(value) LIKE ?",
                    ]
                )
                params.extend([pattern, pattern, pattern, pattern])
            sql += " WHERE " + " OR ".join(conditions)
        sql += " ORDER BY importance DESC, last_seen DESC LIMIT ?"
        params.append(str(limit if not has_non_ascii else 500))
        with self._session_factory() as connection:
            rows = connection.execute(sql, params).fetchall()
        if has_non_ascii:
            filtered_rows = []
            for row in rows:
                haystack = " ".join(
                    [
                        row["category"] or "",
                        row["subject"] or "",
                        row["predicate"] or "",
                        row["value"] or "",
                    ]
                ).casefold()
                if any(token.casefold() in haystack for token in tokens):
                    filtered_rows.append(row)
            rows = filtered_rows[:limit]
        return [
            MemoryHit(
                id=int(row["id"]),
                user_id=row["user_id"],
                category=row["category"],
                subject=row["subject"],
                predicate=row["predicate"],
                value=row["value"],
                confidence=float(row["confidence"]),
                importance=int(row["importance"]),
                source_ref=row["source_ref"],
            )
            for row in rows
        ]

    def list_facts(self, user_id: str, limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(limit, 500))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, category, subject, predicate, value, confidence, importance, source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                WHERE user_id = ?
                ORDER BY importance DESC, last_seen DESC, id DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
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
    ) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._session_factory() as connection:
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
                    expires_at.isoformat() if expires_at else None,
                    json.dumps(tags or []),
                    user_id,
                    fact_id,
                ),
            )
            connection.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_fact(user_id, fact_id)

    def delete_fact(self, user_id: str, fact_id: int) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute("DELETE FROM memory_facts WHERE user_id = ? AND id = ?", (user_id, fact_id))
            connection.commit()
            return cursor.rowcount > 0

    def get_device_alias_map(self, user_id: str) -> dict[str, str]:
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT subject, value
                FROM memory_facts
                WHERE user_id = ? AND category = ? AND predicate = ?
                ORDER BY importance DESC, last_seen DESC
                """,
                (user_id, "device_alias", "alias"),
            ).fetchall()
        alias_map: dict[str, str] = {}
        for row in rows:
            subject = (row["subject"] or "").strip()
            value = (row["value"] or "").strip()
            if subject and value and subject not in alias_map:
                alias_map[subject] = value
        return alias_map

    def recent_turns(
        self,
        conversation_id: str,
        limit: int = 10,
        hours: int | None = None,
    ) -> list[ConversationTurnRecord]:
        params: list[object] = [conversation_id]
        cutoff_clause = ""
        if hours is not None:
            cutoff_clause = " AND created_at >= ?"
            params.append((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat())
        params.append(limit)
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed
                FROM conversation_turns
                WHERE conversation_id = ?
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
                conversation_id=row["conversation_id"],
                user_id=row["user_id"],
                source=row["source"],
                question=row["question"],
                answer=row["answer"],
                question_created_at=row["question_created_at"],
                answer_created_at=row["answer_created_at"],
                created_at=row["created_at"],
                memory_processed=int(row["memory_processed"]),
            )
            for row in reversed(rows)
        ]

    def old_turns(self, hours: int = 24, limit: int = 500) -> list[ConversationTurnRecord]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, user_id, source, question, answer, question_created_at,
                       answer_created_at, created_at, memory_processed
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
                conversation_id=row["conversation_id"],
                user_id=row["user_id"],
                source=row["source"],
                question=row["question"],
                answer=row["answer"],
                question_created_at=row["question_created_at"],
                answer_created_at=row["answer_created_at"],
                created_at=row["created_at"],
                memory_processed=int(row["memory_processed"]),
            )
            for row in rows
        ]

    def delete_turn(self, turn_id: int) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute("DELETE FROM conversation_turns WHERE id = ?", (turn_id,))
            connection.commit()
            return cursor.rowcount > 0

    def search_embeddings(
        self,
        query_embedding: list[float],
        user_id: str,
        limit: int = 5,
        source_types: list[str] | None = None,
    ) -> list[EmbeddingHit]:
        if not query_embedding:
            return []

        sql = """
            SELECT id, source_type, source_ref, chunk_index, role, content, importance, metadata_json, embedding_json
            FROM memory_embeddings
            WHERE user_id = ?
        """
        params: list[object] = [user_id]
        if source_types:
            placeholders = ", ".join("?" for _ in source_types)
            sql += f" WHERE source_type IN ({placeholders})"
            params.extend(source_types)
        sql += " ORDER BY importance DESC, last_seen DESC, id DESC"

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
                    user_id=user_id,
                    source_type=row["source_type"],
                    source_ref=row["source_ref"],
                    chunk_index=int(row["chunk_index"]),
                    role=row["role"],
                    content=row["content"],
                    importance=int(row["importance"]),
                    similarity=similarity,
                    metadata=self._load_json_object(row["metadata_json"]),
                )
            )

        scored.sort(key=lambda item: (item.similarity, item.importance, item.id), reverse=True)
        return scored[: max(1, limit)]

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
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return dot / (left_norm * right_norm)

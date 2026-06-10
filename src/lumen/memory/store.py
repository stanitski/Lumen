from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone

from lumen.time_utils import local_now_iso

from lumen.models import ConversationTurnRecord, EmbeddingHit, MemoryHit, ReminderRecord


class MemoryStore:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _scope_user_ids(user_id: str) -> tuple[str, str]:
        effective_user_id = (user_id or "").strip() or "lumen-user"
        return effective_user_id, "global"

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
        now = local_now_iso()
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

    def delete_embeddings(self, *, user_id: str, source_type: str, source_ref: str) -> int:
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                DELETE FROM memory_embeddings
                WHERE user_id = ? AND source_type = ? AND source_ref = ?
                """,
                (user_id, source_type, source_ref),
            )
            connection.commit()
            return int(cursor.rowcount)

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
        question_created_at = question_created_at or local_now_iso()
        answer_created_at = answer_created_at or question_created_at
        created_at = created_at or answer_created_at
        with self._session_factory() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    conversation_id, user_id, source, question, answer,
                    question_created_at, answer_created_at, created_at, memory_processed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        now = local_now_iso()
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
        effective_user_id, global_user_id = self._scope_user_ids(user_id)
        sql = """
            SELECT id, user_id, category, subject, predicate, value, confidence, importance, source_ref
            FROM memory_facts
            WHERE user_id IN (?, ?)
        """
        params: list[object] = [effective_user_id, global_user_id]
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
            sql += " AND (" + " OR ".join(conditions) + ")"
        sql += " ORDER BY CASE WHEN user_id = ? THEN 0 ELSE 1 END, importance DESC, last_seen DESC LIMIT ?"
        params.append(effective_user_id)
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
        now = local_now_iso()
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
            params.append((datetime.now().astimezone() - timedelta(hours=hours)).isoformat())
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
        cutoff = (datetime.now().astimezone() - timedelta(hours=hours)).isoformat()
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

    def add_reminder(
        self,
        *,
        user_id: str,
        source_ref: str,
        text: str,
        due_at: str,
        conversation_id: str | None = None,
        repeat_interval_seconds: int | None = None,
        repeat_until: str | None = None,
        status: str = "pending",
        created_at: str | None = None,
        sent_at: str | None = None,
    ) -> int:
        created_at = created_at or local_now_iso()
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reminders(
                    user_id, conversation_id, source_ref, text, due_at, status, created_at, sent_at,
                    repeat_interval_seconds, repeat_until
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    conversation_id,
                    source_ref,
                    text,
                    due_at,
                    status,
                    created_at,
                    sent_at,
                    repeat_interval_seconds,
                    repeat_until,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_reminders(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ReminderRecord]:
        safe_limit = max(1, min(limit, 500))
        params: list[object] = [user_id]
        sql = """
            SELECT id, user_id, conversation_id, source_ref, text, due_at, status, created_at, sent_at,
                   repeat_interval_seconds, repeat_until, metadata_json
            FROM reminders
            WHERE user_id = ?
        """
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY due_at ASC, id ASC LIMIT ?"
        params.append(safe_limit)
        with self._session_factory() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def due_reminders(self, *, limit: int = 100, now: datetime | None = None) -> list[ReminderRecord]:
        safe_limit = max(1, min(limit, 500))
        cutoff = (now or datetime.now().astimezone()).isoformat()
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, conversation_id, source_ref, text, due_at, status, created_at, sent_at,
                       repeat_interval_seconds, repeat_until, metadata_json
                FROM reminders
                WHERE status = ? AND due_at <= ?
                ORDER BY due_at ASC, id ASC
                LIMIT ?
                """,
                ("pending", cutoff, safe_limit),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def mark_reminder_sent(self, reminder_id: int, *, user_id: str, sent_at: str | None = None) -> dict | None:
        return self.set_reminder_status(reminder_id, user_id=user_id, status="sent", sent_at=sent_at)

    def set_reminder_status(
        self,
        reminder_id: int,
        *,
        user_id: str,
        status: str,
        sent_at: str | None = None,
        due_at: str | None = None,
    ) -> dict | None:
        sent_at = sent_at or local_now_iso()
        with self._session_factory() as connection:
            if due_at is None:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = ?, sent_at = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, sent_at, user_id, reminder_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = ?, sent_at = ?, due_at = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, sent_at, due_at, user_id, reminder_id),
                )
            connection.commit()
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT id, user_id, conversation_id, source_ref, text, due_at, status, created_at, sent_at,
                       repeat_interval_seconds, repeat_until, metadata_json
                FROM reminders
                WHERE user_id = ? AND id = ?
                """,
                (user_id, reminder_id),
            ).fetchone()
        return self._row_to_reminder_dict(row) if row else None

    def cancel_reminder(self, reminder_id: int, *, user_id: str) -> bool:
        return self.set_reminder_status(reminder_id, user_id=user_id, status="cancelled") is not None

    def delete_reminder(self, reminder_id: int, *, user_id: str) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute("DELETE FROM reminders WHERE user_id = ? AND id = ?", (user_id, reminder_id))
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

        effective_user_id, global_user_id = self._scope_user_ids(user_id)
        sql = """
            SELECT id, source_type, source_ref, chunk_index, role, content, importance, metadata_json, embedding_json
            FROM memory_embeddings
            WHERE user_id IN (?, ?)
        """
        params: list[object] = [effective_user_id, global_user_id]
        if source_types:
            placeholders = ", ".join("?" for _ in source_types)
            sql += f" AND source_type IN ({placeholders})"
            params.extend(source_types)
        sql += " ORDER BY CASE WHEN user_id = ? THEN 0 ELSE 1 END, importance DESC, last_seen DESC, id DESC"
        params.append(effective_user_id)

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

    def _row_to_reminder(self, row) -> ReminderRecord:
        return ReminderRecord(
            id=int(row["id"]),
            user_id=row["user_id"],
            conversation_id=row["conversation_id"],
            source_ref=row["source_ref"],
            text=row["text"],
            due_at=row["due_at"],
            status=row["status"],
            created_at=row["created_at"],
            sent_at=row["sent_at"],
            repeat_interval_seconds=(
                int(row["repeat_interval_seconds"]) if row["repeat_interval_seconds"] is not None else None
            ),
            repeat_until=row["repeat_until"],
        )

    def _row_to_reminder_dict(self, row) -> dict:
        reminder = self._row_to_reminder(row)
        return reminder.model_dump()

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

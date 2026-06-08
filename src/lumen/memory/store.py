from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from lumen.models import ConversationLogRecord, MemoryHit


class MemoryStore:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def log_message(
        self,
        *,
        conversation_id: str,
        session_id: str,
        user_id: str,
        source: str,
        role: str,
        message: str,
    ) -> None:
        with self._session_factory() as connection:
            connection.execute(
                """
                INSERT INTO conversation_logs(conversation_id, session_id, user_id, source, role, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    session_id,
                    user_id,
                    source,
                    role,
                    message,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

    def add_fact(
        self,
        *,
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
                SELECT id, confidence, importance, tags_json
                FROM memory_facts
                WHERE subject = ? AND predicate = ? AND value = ?
                """,
                (subject, predicate, value),
            ).fetchone()
            if existing:
                merged_tags = sorted(set(json.loads(existing["tags_json"]) + (tags or [])))
                connection.execute(
                    """
                    UPDATE memory_facts
                    SET confidence = ?, importance = ?, last_seen = ?, tags_json = ?, expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        max(existing["confidence"], confidence),
                        max(existing["importance"], importance),
                        now,
                        json.dumps(merged_tags),
                        expires_at.isoformat() if expires_at else None,
                        existing["id"],
                    ),
                )
                connection.commit()
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO memory_facts(category, subject, predicate, value, confidence, importance, source_ref, last_seen, expires_at, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def search(self, query: str, limit: int = 10) -> list[MemoryHit]:
        tokens = [token.strip().lower() for token in re.split(r"\W+", query) if token.strip()]
        has_non_ascii = any(any(ord(char) > 127 for char in token) for token in tokens)
        sql = """
            SELECT id, category, subject, predicate, value, confidence, importance, source_ref
            FROM memory_facts
        """
        params: list[str] = []
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

    def list_facts(self, limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(limit, 500))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, category, subject, predicate, value, confidence, importance, source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                ORDER BY importance DESC, last_seen DESC, id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._row_to_fact_dict(row) for row in rows]

    def get_fact(self, fact_id: int) -> dict | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, category, subject, predicate, value, confidence, importance, source_ref, last_seen, expires_at, tags_json
                FROM memory_facts
                WHERE id = ?
                """,
                (fact_id,),
            ).fetchone()
        return self._row_to_fact_dict(row) if row else None

    def update_fact(
        self,
        fact_id: int,
        *,
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
                WHERE id = ?
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
                    fact_id,
                ),
            )
            connection.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_fact(fact_id)

    def delete_fact(self, fact_id: int) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))
            connection.commit()
            return cursor.rowcount > 0

    def get_device_alias_map(self) -> dict[str, str]:
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT subject, value
                FROM memory_facts
                WHERE category = ? AND predicate = ?
                ORDER BY importance DESC, last_seen DESC
                """,
                ("device_alias", "alias"),
            ).fetchall()
        alias_map: dict[str, str] = {}
        for row in rows:
            subject = (row["subject"] or "").strip()
            value = (row["value"] or "").strip()
            if subject and value and subject not in alias_map:
                alias_map[subject] = value
        return alias_map

    def extract_facts_from_text(self, text: str, source_ref: str) -> list[int]:
        extracted: list[int] = []
        for fact_input in self._candidate_facts(text):
            stored_id = self.add_fact(source_ref=source_ref, **fact_input)
            extracted.append(stored_id)
        return extracted

    def _candidate_facts(self, text: str) -> list[dict]:
        normalized = text.strip()
        if not normalized or len(normalized) < 12:
            return []

        results: list[dict] = []
        results.extend(self._ukrainian_candidate_facts(normalized, normalized.lower()))

        patterns: list[tuple[re.Pattern[str], dict]] = [
            (
                re.compile(r"\bI prefer (?P<value>.+)", re.IGNORECASE),
                {
                    "category": "preference",
                    "subject": "user",
                    "predicate": "prefers",
                    "importance": 7,
                    "confidence": 0.85,
                    "min_value_len": 8,
                },
            ),
            (
                re.compile(r"\bmy (?P<subject>[\w\s]+?) is (?P<value>.+)", re.IGNORECASE),
                {
                    "category": "profile",
                    "predicate": "is",
                    "importance": 6,
                    "confidence": 0.75,
                    "min_value_len": 3,
                },
            ),
            (
                re.compile(r"\b(?:remember|note) that (?P<value>.+)", re.IGNORECASE),
                {
                    "category": "rule",
                    "subject": "user",
                    "predicate": "remember",
                    "importance": 8,
                    "confidence": 0.9,
                    "min_value_len": 8,
                },
            ),
            (
                re.compile(r"\b(?P<subject>[a-z0-9_]+\.[a-z0-9_]+)\s+(?:is|means)\s+(?P<value>.+)", re.IGNORECASE),
                {
                    "category": "device_alias",
                    "predicate": "alias",
                    "importance": 8,
                    "confidence": 0.9,
                    "min_value_len": 2,
                },
            ),
        ]

        for pattern, defaults in patterns:
            match = pattern.search(normalized)
            if not match:
                continue
            value = match.groupdict().get("value", "").strip().rstrip(".")
            if len(value) < defaults.get("min_value_len", 8):
                continue
            results.append(
                {
                    "category": defaults["category"],
                    "subject": match.groupdict().get("subject", defaults.get("subject", "user")).strip(),
                    "predicate": defaults["predicate"],
                    "value": value,
                    "confidence": defaults["confidence"],
                    "importance": defaults["importance"],
                    "tags": [defaults["category"]],
                }
            )
        return results

    def _ukrainian_candidate_facts(self, normalized: str, lowered: str) -> list[dict]:
        results: list[dict] = []

        preference_phrases = [
            "\u044f \u043b\u044e\u0431\u043b\u044e ",
            "\u043c\u0435\u043d\u0456 \u043f\u043e\u0434\u043e\u0431\u0430\u0454\u0442\u044c\u0441\u044f ",
            "\u044f \u043d\u0430\u0434\u0430\u044e \u043f\u0435\u0440\u0435\u0432\u0430\u0433\u0443 ",
        ]
        for phrase in preference_phrases:
            value = self._slice_after_phrase(normalized, lowered, phrase)
            if value:
                results.append(
                    {
                        "category": "preference",
                        "subject": "user",
                        "predicate": "prefers",
                        "value": value,
                        "confidence": 0.85,
                        "importance": 7,
                        "tags": ["preference"],
                    }
                )
                break

        name_phrase = "\u043c\u0435\u043d\u0435 \u0437\u0432\u0430\u0442\u0438 "
        value = self._slice_after_phrase(normalized, lowered, name_phrase)
        if value:
            results.append(
                {
                    "category": "profile",
                    "subject": "user_name",
                    "predicate": "is",
                    "value": value,
                    "confidence": 0.9,
                    "importance": 8,
                    "tags": ["profile"],
                }
            )

        remember_phrases = [
            "\u0437\u0430\u043f\u0430\u043c'\u044f\u0442\u0430\u0439, \u0449\u043e ",
            "\u0437\u0430\u043f\u0430\u043c'\u044f\u0442\u0430\u0439 \u0449\u043e ",
            "\u0437\u0430\u043f\u0430\u043c\u2019\u044f\u0442\u0430\u0439, \u0449\u043e ",
            "\u0437\u0430\u043f\u0430\u043c\u2019\u044f\u0442\u0430\u0439 \u0449\u043e ",
            "\u0437\u0430\u043f\u0438\u0448\u0438, \u0449\u043e ",
            "\u0437\u0430\u043f\u0438\u0448\u0438 \u0449\u043e ",
            "\u043d\u043e\u0442\u0430\u0442\u043a\u0430: ",
        ]
        for phrase in remember_phrases:
            value = self._slice_after_phrase(normalized, lowered, phrase)
            if value:
                results.append(
                    {
                        "category": "rule",
                        "subject": "user",
                        "predicate": "remember",
                        "value": value,
                        "confidence": 0.9,
                        "importance": 8,
                        "tags": ["rule"],
                    }
                )
                break

        alias_patterns = [
            re.compile(r"(?P<subject>[a-z0-9_]+\.[a-z0-9_]+)\s+це\s+(?P<value>.+)", re.IGNORECASE),
            re.compile(r"запам['’]ятай,?\s+що\s+(?P<subject>[a-z0-9_]+\.[a-z0-9_]+)\s+це\s+(?P<value>.+)", re.IGNORECASE),
        ]
        for pattern in alias_patterns:
            match = pattern.search(normalized)
            if not match:
                continue
            value = match.group("value").strip().rstrip(".")
            if len(value) < 2:
                continue
            results.append(
                {
                    "category": "device_alias",
                    "subject": match.group("subject").strip(),
                    "predicate": "alias",
                    "value": value,
                    "confidence": 0.9,
                    "importance": 8,
                    "tags": ["device_alias"],
                }
            )
            break

        return results

    def _slice_after_phrase(self, normalized: str, lowered: str, phrase: str) -> str:
        index = lowered.find(phrase)
        if index == -1:
            return ""
        value = normalized[index + len(phrase) :].strip().rstrip(".")
        return value if len(value) >= 2 else ""

    def recent_conversation(self, conversation_id: str, limit: int = 10) -> list[ConversationLogRecord]:
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, session_id, user_id, source, role, message, created_at
                FROM conversation_logs
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            ConversationLogRecord(
                id=int(row["id"]),
                conversation_id=row["conversation_id"],
                session_id=row["session_id"],
                user_id=row["user_id"],
                source=row["source"],
                role=row["role"],
                message=row["message"],
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        ]

    def _row_to_fact_dict(self, row) -> dict:
        return {
            "id": int(row["id"]),
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

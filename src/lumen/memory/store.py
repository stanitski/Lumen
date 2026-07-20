"""SQLite-backed persistence layer for turns, facts, embeddings, and reminders."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from lumen.time_utils import local_now_iso

from lumen.models import (
    ConversationTurnRecord,
    EmbeddingHit,
    HomeAssistantEntityRecord,
    IntentRecord,
    KnowledgeUploadSessionRecord,
    ReminderRecord,
    SkillRecord,
    TaskRecord,
    TaskRunRecord,
)


class MemoryStore:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def upsert_intent(self, intent: IntentRecord) -> None:
        with self._session_factory() as connection:
            connection.execute(
                """
                INSERT INTO intents(
                    intent_id, kind, enabled, description, is_default, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO UPDATE SET
                    kind = excluded.kind,
                    enabled = excluded.enabled,
                    description = excluded.description,
                    is_default = excluded.is_default,
                    updated_at = excluded.updated_at
                """,
                (
                    intent.intent_id,
                    intent.kind,
                    int(intent.enabled),
                    intent.description,
                    int(intent.is_default),
                    intent.created_at,
                    intent.updated_at,
                ),
            )
            connection.commit()

    def list_intents(self, *, kind: str | None = None, enabled_only: bool = True) -> list[IntentRecord]:
        sql = """
            SELECT intent_id, kind, enabled, description, is_default, created_at, updated_at
            FROM intents
            WHERE 1 = 1
        """
        params: list[object] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind.strip())
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY kind ASC, is_default DESC, intent_id ASC"
        with self._session_factory() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_intent(row) for row in rows]

    def default_intent(self, *, kind: str) -> IntentRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT intent_id, kind, enabled, description, is_default, created_at, updated_at
                FROM intents
                WHERE kind = ? AND enabled = 1
                ORDER BY is_default DESC, intent_id ASC
                LIMIT 1
                """,
                (kind.strip(),),
            ).fetchone()
        return self._row_to_intent(row) if row else None

    def upsert_skill(self, skill: SkillRecord) -> None:
        with self._session_factory() as connection:
            connection.execute(
                """
                INSERT INTO skills(
                    skill_id, intent_id, scope, owner_user_id, enabled, priority, description, tags_json,
                    variables_schema_json, steps_json, embedding_json, content_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    intent_id = excluded.intent_id,
                    scope = excluded.scope,
                    owner_user_id = excluded.owner_user_id,
                    enabled = excluded.enabled,
                    priority = excluded.priority,
                    description = excluded.description,
                    tags_json = excluded.tags_json,
                    variables_schema_json = excluded.variables_schema_json,
                    steps_json = excluded.steps_json,
                    embedding_json = excluded.embedding_json,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    skill.skill_id,
                    skill.intent_id,
                    skill.scope,
                    skill.owner_user_id,
                    int(skill.enabled),
                    int(skill.priority),
                    skill.description,
                    json.dumps(skill.tags, ensure_ascii=False),
                    json.dumps(skill.variables_schema, ensure_ascii=False),
                    json.dumps(skill.steps, ensure_ascii=False),
                    json.dumps([float(value) for value in skill.embedding], ensure_ascii=False),
                    skill.content_hash,
                    skill.created_at,
                    skill.updated_at,
                ),
            )
            connection.commit()

    def get_skill(self, skill_id: str) -> SkillRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT skill_id, intent_id, scope, owner_user_id, enabled, priority, description, tags_json,
                       variables_schema_json, steps_json, embedding_json, content_hash, created_at, updated_at
                FROM skills
                WHERE skill_id = ?
                """,
                (skill_id.strip(),),
            ).fetchone()
        return self._row_to_skill(row) if row else None

    def set_skill_enabled(self, skill_id: str, *, enabled: bool) -> bool:
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE skills
                SET enabled = ?, updated_at = ?
                WHERE skill_id = ?
                """,
                (int(enabled), local_now_iso(), skill_id.strip()),
            )
            connection.commit()
            return int(cursor.rowcount) > 0

    def list_skills(
        self,
        *,
        scope: str | None = None,
        owner_user_id: str | None = None,
        enabled_only: bool = True,
        limit: int = 500,
    ) -> list[SkillRecord]:
        safe_limit = max(1, min(limit, 5000))
        sql = """
            SELECT skill_id, intent_id, scope, owner_user_id, enabled, priority, description, tags_json,
                   variables_schema_json, steps_json, embedding_json, content_hash, created_at, updated_at
            FROM skills
            WHERE 1 = 1
        """
        params: list[object] = []
        if scope:
            sql += " AND scope = ?"
            params.append(scope.strip())
        if owner_user_id is not None:
            sql += " AND owner_user_id = ?"
            params.append(owner_user_id.strip())
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY priority DESC, skill_id ASC LIMIT ?"
        params.append(safe_limit)
        with self._session_factory() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_skill(row) for row in rows]

    def update_skill_embedding(
        self,
        *,
        skill_id: str,
        embedding: list[float],
        content_hash: str,
        updated_at: str | None = None,
    ) -> bool:
        now = updated_at or local_now_iso()
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE skills
                SET embedding_json = ?, content_hash = ?, updated_at = ?
                WHERE skill_id = ?
                """,
                (
                    json.dumps([float(value) for value in embedding], ensure_ascii=False),
                    content_hash,
                    now,
                    skill_id.strip(),
                ),
            )
            connection.commit()
            return int(cursor.rowcount) > 0

    def search_skills_by_embedding(
        self,
        query_embedding: list[float],
        limit: int | None = 5,
    ) -> list[dict[str, object]]:
        if not query_embedding:
            return []
        safe_limit = max(1, min(limit, 100))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT skill_id, embedding_json, priority
                FROM skills
                WHERE enabled = 1
                  AND embedding_json IS NOT NULL
                  AND embedding_json != '[]'
                """,
            ).fetchall()

        scored: list[dict[str, object]] = []
        for row in rows:
            stored_embedding = self._load_embedding_vector(row["embedding_json"])
            if not stored_embedding:
                continue
            similarity = self._cosine_similarity(query_embedding, stored_embedding)
            scored.append(
                {
                    "skill_id": row["skill_id"],
                    "similarity": similarity,
                    "priority": int(row["priority"]),
                }
            )
        scored.sort(key=lambda item: (float(item["similarity"]), int(item["priority"])), reverse=True)
        return scored[:safe_limit]

    def create_task(
        self,
        *,
        user_id: str,
        source_ref: str,
        skill_id: str | None = None,
        task_group_id: str | None = None,
        parent_task_id: int | None = None,
        status: str = "pending",
        execution_mode: str = "immediate",
        due_at: str | None = None,
        repeat_policy: str | None = None,
        repeat_interval_seconds: int | None = None,
        repeat_count: int | None = None,
        repeat_until: str | None = None,
        priority: int = 50,
        payload: dict[str, object] | None = None,
        steps: list[dict[str, object]] | None = None,
        response_policy: str | None = None,
        requires_confirmation: bool = False,
        created_at: str | None = None,
    ) -> int:
        now = created_at or local_now_iso()
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tasks(
                    user_id, source_ref, skill_id, task_group_id, parent_task_id, status, execution_mode, due_at,
                    repeat_policy, repeat_interval_seconds, repeat_count, repeat_until, run_count, priority,
                    payload_json, steps_json, requires_confirmation, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source_ref,
                    skill_id,
                    task_group_id,
                    parent_task_id,
                    status,
                    execution_mode,
                    due_at,
                    repeat_policy,
                    repeat_interval_seconds,
                    repeat_count,
                    repeat_until,
                    0,
                    int(priority),
                    json.dumps(payload or {}, ensure_ascii=False),
                    json.dumps(steps or [], ensure_ascii=False),
                    int(requires_confirmation),
                    now,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_task(self, task_id: int) -> TaskRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source_ref, skill_id, task_group_id, parent_task_id, status, execution_mode, due_at,
                       repeat_policy, repeat_interval_seconds, repeat_count, repeat_until, run_count, priority,
                       payload_json, steps_json, requires_confirmation, created_at, updated_at,
                       completed_at, cancelled_at, last_error
                FROM tasks
                WHERE id = ?
                """,
                (int(task_id),),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list_due_tasks(self, *, now_iso: str, limit: int = 100) -> list[TaskRecord]:
        safe_limit = max(1, min(limit, 500))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source_ref, skill_id, task_group_id, parent_task_id, status, execution_mode, due_at,
                       repeat_policy, repeat_interval_seconds, repeat_count, repeat_until, run_count, priority,
                       payload_json, steps_json, requires_confirmation, created_at, updated_at,
                       completed_at, cancelled_at, last_error
                FROM tasks
                WHERE status = 'pending'
                  AND (execution_mode = 'immediate' OR due_at IS NULL OR due_at <= ?)
                ORDER BY priority DESC, due_at ASC, id ASC
                LIMIT ?
                """,
                (now_iso, safe_limit),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_tasks(
        self,
        *,
        user_id: str,
        statuses: list[str] | None = None,
        skill_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        safe_limit = max(1, min(limit, 500))
        clauses = ["user_id = ?"]
        params: list[object] = [user_id]
        normalized_statuses = [status.strip() for status in (statuses or []) if status.strip()]
        if normalized_statuses:
            placeholders = ", ".join("?" for _ in normalized_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(normalized_statuses)
        if skill_id:
            clauses.append("skill_id = ?")
            params.append(skill_id.strip())
        params.append(safe_limit)
        with self._session_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT id, user_id, source_ref, skill_id, task_group_id, parent_task_id, status, execution_mode, due_at,
                       repeat_policy, repeat_interval_seconds, repeat_count, repeat_until, run_count, priority,
                       payload_json, steps_json, requires_confirmation, created_at, updated_at,
                       completed_at, cancelled_at, last_error
                FROM tasks
                WHERE {' AND '.join(clauses)}
                ORDER BY due_at ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def latest_awaiting_input_task(self, *, user_id: str) -> TaskRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source_ref, skill_id, task_group_id, parent_task_id, status, execution_mode, due_at,
                       repeat_policy, repeat_interval_seconds, repeat_count, repeat_until, run_count, priority,
                       payload_json, steps_json, requires_confirmation, created_at, updated_at,
                       completed_at, cancelled_at, last_error
                FROM tasks
                WHERE user_id = ? AND status = 'awaiting_input'
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def create_task_run(
        self,
        *,
        task_id: int,
        task_group_id: str | None = None,
        status: str = "queued",
        started_at: str | None = None,
        result: dict[str, object] | None = None,
        error_text: str = "",
        cancelled_by_task_id: int | None = None,
    ) -> int:
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO task_runs(
                    task_id, task_group_id, status, started_at, finished_at, result_json, error_text,
                    cancelled_by_task_id
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    int(task_id),
                    task_group_id,
                    status,
                    started_at,
                    json.dumps(result or {}, ensure_ascii=False),
                    error_text,
                    cancelled_by_task_id,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_task_run(
        self,
        run_id: int,
        *,
        status: str,
        result: dict[str, object] | None = None,
        error_text: str = "",
        finished_at: str | None = None,
    ) -> None:
        now = finished_at or local_now_iso()
        with self._session_factory() as connection:
            connection.execute(
                """
                UPDATE task_runs
                SET status = ?, finished_at = ?, result_json = ?, error_text = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    json.dumps(result or {}, ensure_ascii=False),
                    error_text,
                    int(run_id),
                ),
            )
            connection.commit()

    def update_task_status(
        self,
        task_id: int,
        *,
        status: str,
        completed_at: str | None = None,
        cancelled_at: str | None = None,
        last_error: str = "",
    ) -> None:
        now = local_now_iso()
        with self._session_factory() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, completed_at = COALESCE(?, completed_at),
                    cancelled_at = COALESCE(?, cancelled_at), last_error = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    completed_at,
                    cancelled_at,
                    last_error,
                    int(task_id),
                ),
            )
            connection.commit()

    def mark_task_active(self, task_id: int) -> None:
        now = local_now_iso()
        with self._session_factory() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'active', updated_at = ?
                WHERE id = ?
                """,
                (now, int(task_id)),
            )
            connection.commit()

    def finish_task_after_run(
        self,
        task_id: int,
        *,
        status: str,
        next_due_at: str | None = None,
        last_error: str = "",
    ) -> None:
        now = local_now_iso()
        completed_at = now if status == "completed" else None
        cancelled_at = now if status == "cancelled" else None
        with self._session_factory() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, due_at = ?, run_count = run_count + 1, updated_at = ?,
                    completed_at = COALESCE(?, completed_at),
                    cancelled_at = COALESCE(?, cancelled_at),
                    last_error = ?
                WHERE id = ?
                """,
                (
                    status,
                    next_due_at,
                    now,
                    completed_at,
                    cancelled_at,
                    last_error,
                    int(task_id),
                ),
            )
            connection.commit()

    def cancel_tasks(
        self,
        *,
        task_id: int | None = None,
        task_group_id: str | None = None,
        skill_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        clauses = ["status IN ('pending', 'active')"]
        params: list[object] = []
        if task_id is not None:
            clauses.append("id = ?")
            params.append(int(task_id))
        if task_group_id:
            clauses.append("task_group_id = ?")
            params.append(task_group_id)
        if skill_id:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if len(clauses) == 1:
            return 0

        now = local_now_iso()
        with self._session_factory() as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks
                SET status = 'cancelled', cancelled_at = ?, updated_at = ?
                WHERE {' AND '.join(clauses)}
                """,
                [now, now, *params],
            )
            connection.commit()
            return int(cursor.rowcount)

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
        limit: int = 100,
        source_types: list[str] | None = None,
        source_ref_prefixes: list[str] | None = None,
    ) -> list[EmbeddingHit]:
        safe_limit = max(1, min(int(limit), 1000))
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
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
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

    def delete_fact_ids(self, *, user_id: str, fact_ids: list[int]) -> int:
        ids = [int(item) for item in fact_ids if int(item) > 0]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._session_factory() as connection:
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

    def find_routine(self, *, user_id: str, query: str) -> dict[str, object] | None:
        normalized_query = self._normalize_text(query)
        if not normalized_query:
            return None
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, subject, predicate, value, confidence, importance, source_ref, last_seen, tags_json
                FROM memory_facts
                WHERE user_id IN (?, 'global') AND category = ?
                ORDER BY CASE WHEN user_id = ? THEN 0 ELSE 1 END, importance DESC, last_seen DESC, id DESC
                LIMIT 100
                """,
                (user_id, "routine", user_id),
            ).fetchall()

        best: dict[str, object] | None = None
        best_score = 0.0
        for row in rows:
            try:
                payload = json.loads(row["value"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            names = self._routine_match_names(row["subject"], payload)
            score = self._score_routine_match(normalized_query, names)
            if score <= best_score:
                continue
            steps = payload.get("steps")
            if not isinstance(steps, list):
                continue
            best_score = score
            best = {
                "id": int(row["id"]),
                "user_id": row["user_id"],
                "name": str(payload.get("name") or row["subject"] or "").strip(),
                "aliases": [name for name in names if name],
                "steps": [step for step in steps if isinstance(step, dict)],
                "payload": payload,
                "score": score,
                "source_ref": row["source_ref"],
            }
        return best if best_score >= 1.0 else None

    def list_routines(self, *, user_id: str, include_global: bool = True, limit: int = 100) -> list[dict[str, object]]:
        safe_limit = max(1, min(limit, 500))
        if include_global:
            where = "user_id IN (?, 'global')"
            params: list[object] = [user_id, "routine", user_id, safe_limit]
            order_scope = "CASE WHEN user_id = ? THEN 0 ELSE 1 END,"
        else:
            where = "user_id = ?"
            params = [user_id, "routine", safe_limit]
            order_scope = ""
        with self._session_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT id, user_id, subject, predicate, value, confidence, importance, source_ref, last_seen, tags_json
                FROM memory_facts
                WHERE {where} AND category = ?
                ORDER BY {order_scope} importance DESC, last_seen DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        routines: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["value"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            routines.append(
                {
                    "id": int(row["id"]),
                    "user_id": row["user_id"],
                    "subject": row["subject"],
                    "predicate": row["predicate"],
                    "name": str(payload.get("name") or row["subject"] or "").strip(),
                    "aliases": self._routine_match_names(row["subject"], payload),
                    "steps": payload.get("steps") if isinstance(payload.get("steps"), list) else [],
                    "payload": payload,
                    "source_ref": row["source_ref"],
                    "last_seen": row["last_seen"],
                }
            )
        return routines

    @classmethod
    def _routine_match_names(cls, subject: object, payload: dict[str, object]) -> list[str]:
        names: list[str] = []
        for value in (subject, payload.get("name")):
            text = str(value or "").strip()
            if text:
                names.append(text)
        aliases = payload.get("aliases")
        if isinstance(aliases, list):
            names.extend(str(alias).strip() for alias in aliases if str(alias).strip())
        return names

    @classmethod
    def _score_routine_match(cls, normalized_query: str, names: list[str]) -> float:
        score = 0.0
        for name in names:
            normalized_name = cls._normalize_text(name)
            if not normalized_name:
                continue
            if normalized_query == normalized_name:
                score = max(score, 10.0)
            elif normalized_name in normalized_query or normalized_query in normalized_name:
                score = max(score, 5.0)
        return score

    @staticmethod
    def _normalize_text(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    def replace_ha_entities(self, entities: list[HomeAssistantEntityRecord]) -> int:
        if not entities:
            with self._session_factory() as connection:
                connection.execute("DELETE FROM ha_entities")
                connection.commit()
            return 0

        with self._session_factory() as connection:
            connection.execute("DELETE FROM ha_entities")
            connection.executemany(
                """
                INSERT INTO ha_entities(
                    entity_id, domain, state, friendly_name, unit_of_measurement, device_class,
                    area_name, device_name, aliases_json, attributes_json, search_text,
                    is_readable, is_actionable, last_synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        entity.entity_id,
                        entity.domain,
                        entity.state,
                        entity.friendly_name,
                        entity.unit_of_measurement,
                        entity.device_class,
                        entity.area_name,
                        entity.device_name,
                        json.dumps(entity.aliases, ensure_ascii=False),
                        json.dumps(entity.attributes, ensure_ascii=False),
                        entity.search_text,
                        1 if entity.is_readable else 0,
                        1 if entity.is_actionable else 0,
                        entity.last_synced_at,
                    )
                    for entity in entities
                ],
            )
            connection.commit()
        return len(entities)

    def get_ha_entity(self, entity_id: str) -> HomeAssistantEntityRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT entity_id, domain, state, friendly_name, unit_of_measurement, device_class,
                       area_name, device_name, aliases_json, attributes_json, search_text,
                       is_readable, is_actionable, last_synced_at
                FROM ha_entities
                WHERE entity_id = ?
                """,
                (entity_id,),
            ).fetchone()
        return self._row_to_ha_entity(row) if row else None

    def list_ha_entities(
        self,
        *,
        domains: list[str] | None = None,
        actionable_only: bool = False,
        readable_only: bool = False,
        limit: int = 1000,
    ) -> list[HomeAssistantEntityRecord]:
        safe_limit = max(1, min(limit, 5000))
        sql = """
            SELECT entity_id, domain, state, friendly_name, unit_of_measurement, device_class,
                   area_name, device_name, aliases_json, attributes_json, search_text,
                   is_readable, is_actionable, last_synced_at
            FROM ha_entities
            WHERE 1 = 1
        """
        params: list[object] = []
        normalized_domains = [item.strip().lower() for item in (domains or []) if item.strip()]
        if normalized_domains:
            placeholders = ", ".join("?" for _ in normalized_domains)
            sql += f" AND domain IN ({placeholders})"
            params.extend(normalized_domains)
        if actionable_only:
            sql += " AND is_actionable = 1"
        if readable_only:
            sql += " AND is_readable = 1"
        sql += " ORDER BY domain ASC, friendly_name ASC, entity_id ASC LIMIT ?"
        params.append(safe_limit)
        with self._session_factory() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_ha_entity(row) for row in rows]

    def last_ha_entities_sync_at(self) -> str | None:
        with self._session_factory() as connection:
            row = connection.execute("SELECT MAX(last_synced_at) AS last_synced_at FROM ha_entities").fetchone()
        if row is None or not row["last_synced_at"]:
            return None
        return str(row["last_synced_at"])

    def migrate_user_id_values(self, *, old_user_ids: list[str], new_user_id: str) -> dict[str, int]:
        normalized_old_user_ids = [item.strip() for item in old_user_ids if item and item.strip()]
        normalized_new_user_id = new_user_id.strip()
        if not normalized_old_user_ids or not normalized_new_user_id:
            return {}
        tables = [
            "conversation_turns",
            "memory_facts",
            "memory_embeddings",
            "reminders",
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
    ) -> list[ConversationTurnRecord]:
        params: list[object] = [user_id]
        cutoff_clause = ""
        if hours is not None:
            cutoff_clause = " AND created_at >= ?"
            params.append((datetime.now().astimezone() - timedelta(hours=hours)).isoformat())
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

    def add_reminder(
        self,
        *,
        user_id: str,
        source_ref: str,
        text: str,
        due_at: str,
        repeat_interval_seconds: int | None = None,
        repeat_until: str | None = None,
        status: str = "pending",
        created_at: str | None = None,
        sent_at: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> int:
        created_at = created_at or local_now_iso()
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reminders(
                    user_id, source_ref, text, due_at, status, created_at, sent_at,
                    repeat_interval_seconds, repeat_until, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source_ref,
                    text,
                    due_at,
                    status,
                    created_at,
                    sent_at,
                    repeat_interval_seconds,
                    repeat_until,
                    json.dumps(metadata or {}, ensure_ascii=False),
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
            SELECT id, user_id, source_ref, text, due_at, status, created_at, sent_at,
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
                SELECT id, user_id, source_ref, text, due_at, status, created_at, sent_at,
                       repeat_interval_seconds, repeat_until, metadata_json
                FROM reminders
                WHERE (
                    status IN (?, ?)
                    OR (status = ? AND repeat_interval_seconds IS NOT NULL)
                )
                AND due_at <= ?
                ORDER BY due_at ASC, id ASC
                LIMIT ?
                """,
                ("pending", "failed", "awaiting_confirmation", cutoff, safe_limit),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def awaiting_confirmation_reminders(self, *, limit: int = 500) -> list[ReminderRecord]:
        safe_limit = max(1, min(limit, 1000))
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, source_ref, text, due_at, status, created_at, sent_at,
                       repeat_interval_seconds, repeat_until, metadata_json
                FROM reminders
                WHERE status = ?
                ORDER BY sent_at ASC, due_at ASC, id ASC
                LIMIT ?
                """,
                ("awaiting_confirmation", safe_limit),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def next_pending_reminder_due_at(self) -> str | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT due_at
                FROM reminders
                WHERE status IN (?, ?)
                   OR (status = ? AND repeat_interval_seconds IS NOT NULL)
                ORDER BY due_at ASC, id ASC
                LIMIT 1
                """,
                ("pending", "failed", "awaiting_confirmation"),
            ).fetchone()
        return str(row["due_at"]) if row and row["due_at"] else None

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
        metadata: dict[str, object] | None = None,
    ) -> dict | None:
        sent_at = sent_at or local_now_iso()
        with self._session_factory() as connection:
            if due_at is None and metadata is None:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = ?, sent_at = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, sent_at, user_id, reminder_id),
                )
            elif due_at is None:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = ?, sent_at = ?, metadata_json = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, sent_at, json.dumps(metadata or {}, ensure_ascii=False), user_id, reminder_id),
                )
            elif metadata is None:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = ?, sent_at = ?, due_at = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, sent_at, due_at, user_id, reminder_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE reminders
                    SET status = ?, sent_at = ?, due_at = ?, metadata_json = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (status, sent_at, due_at, json.dumps(metadata or {}, ensure_ascii=False), user_id, reminder_id),
                )
            connection.commit()
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT id, user_id, source_ref, text, due_at, status, created_at, sent_at,
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

    def get_reminder(self, reminder_id: int, *, user_id: str) -> ReminderRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source_ref, text, due_at, status, created_at, sent_at,
                       repeat_interval_seconds, repeat_until, metadata_json
                FROM reminders
                WHERE user_id = ? AND id = ?
                """,
                (user_id, reminder_id),
            ).fetchone()
        return self._row_to_reminder(row) if row else None

    def get_reminder_by_source_ref(self, *, user_id: str, source_ref: str) -> ReminderRecord | None:
        with self._session_factory() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, source_ref, text, due_at, status, created_at, sent_at,
                       repeat_interval_seconds, repeat_until, metadata_json
                FROM reminders
                WHERE user_id = ? AND source_ref = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, source_ref),
            ).fetchone()
        return self._row_to_reminder(row) if row else None

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

        effective_user_id = (user_id or "").strip() or "VST"
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
            metadata=self._load_json_object(row["metadata_json"]),
        )

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

    def _row_to_ha_entity(self, row) -> HomeAssistantEntityRecord:
        aliases_value = self._load_json_array(row["aliases_json"])
        aliases = [str(item).strip() for item in aliases_value if str(item).strip()]
        return HomeAssistantEntityRecord(
            entity_id=row["entity_id"],
            domain=row["domain"],
            state=row["state"],
            friendly_name=row["friendly_name"],
            unit_of_measurement=row["unit_of_measurement"],
            device_class=row["device_class"],
            area_name=row["area_name"],
            device_name=row["device_name"],
            aliases=aliases,
            attributes=self._load_json_object(row["attributes_json"]),
            search_text=row["search_text"],
            is_readable=bool(int(row["is_readable"])),
            is_actionable=bool(int(row["is_actionable"])),
            last_synced_at=row["last_synced_at"],
        )

    def _row_to_skill(self, row) -> SkillRecord:
        return SkillRecord(
            skill_id=row["skill_id"],
            intent_id=row["intent_id"],
            scope=row["scope"],
            owner_user_id=row["owner_user_id"],
            enabled=bool(int(row["enabled"])),
            priority=int(row["priority"]),
            description=row["description"],
            tags=[str(item) for item in self._load_json_array(row["tags_json"])],
            variables_schema=self._load_json_object(row["variables_schema_json"]),
            steps=[
                item
                for item in self._load_json_array(row["steps_json"])
                if isinstance(item, dict)
            ],
            embedding=[float(value) for value in self._load_embedding_vector(row["embedding_json"])],
            content_hash=row["content_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_intent(self, row) -> IntentRecord:
        return IntentRecord(
            intent_id=row["intent_id"],
            kind=row["kind"],
            enabled=bool(int(row["enabled"])),
            description=row["description"],
            is_default=bool(int(row["is_default"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_task(self, row) -> TaskRecord:
        return TaskRecord(
            id=int(row["id"]),
            user_id=row["user_id"],
            source_ref=row["source_ref"],
            skill_id=row["skill_id"],
            task_group_id=row["task_group_id"],
            parent_task_id=int(row["parent_task_id"]) if row["parent_task_id"] is not None else None,
            status=row["status"],
            execution_mode=row["execution_mode"],
            due_at=row["due_at"],
            repeat_policy=row["repeat_policy"],
            repeat_interval_seconds=(
                int(row["repeat_interval_seconds"]) if row["repeat_interval_seconds"] is not None else None
            ),
            repeat_count=int(row["repeat_count"]) if row["repeat_count"] is not None else None,
            repeat_until=row["repeat_until"],
            run_count=int(row["run_count"]),
            priority=int(row["priority"]),
            payload=self._load_json_object(row["payload_json"]),
            steps=[
                item
                for item in self._load_json_array(row["steps_json"])
                if isinstance(item, dict)
            ],
            requires_confirmation=bool(int(row["requires_confirmation"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            cancelled_at=row["cancelled_at"],
            last_error=row["last_error"],
        )

    def _row_to_task_run(self, row) -> TaskRunRecord:
        return TaskRunRecord(
            id=int(row["id"]),
            task_id=int(row["task_id"]),
            task_group_id=row["task_group_id"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            result=self._load_json_object(row["result_json"]),
            error_text=row["error_text"],
            cancelled_by_task_id=(
                int(row["cancelled_by_task_id"]) if row["cancelled_by_task_id"] is not None else None
            ),
        )

    def _row_to_reminder_dict(self, row) -> dict:
        reminder = self._row_to_reminder(row)
        return reminder.model_dump()

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
    def _load_json_array(raw_json: str) -> list[object]:
        try:
            value = json.loads(raw_json or "[]")
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

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

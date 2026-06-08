from __future__ import annotations

import json
import re
from pathlib import Path

from lumen.models import KnowledgeHit, utc_now


class KnowledgeStore:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def ingest_path(self, path: str) -> int:
        target = Path(path)
        indexed = 0
        with self._session_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ingestion_runs(source_path, status, documents_indexed, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(target), "running", 0, "", utc_now().isoformat()),
            )
            run_id = int(cursor.lastrowid)
            connection.commit()
            try:
                files = self._iter_files(target)
                for file_path in files:
                    content = self._read_file(file_path)
                    if not content.strip():
                        continue
                    source_type = self._source_type_for(file_path)
                    title = file_path.name
                    source_ref = str(file_path.resolve())
                    existing = connection.execute(
                        "SELECT id FROM knowledge_documents WHERE source_ref = ?",
                        (source_ref,),
                    ).fetchone()
                    now = utc_now().isoformat()
                    if existing is None:
                        doc_cursor = connection.execute(
                            """
                            INSERT INTO knowledge_documents(source_type, source_ref, title, content, extra_metadata_json, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source_type,
                                source_ref,
                                title,
                                content,
                                json.dumps({"path": source_ref}),
                                now,
                                now,
                            ),
                        )
                        document_id = int(doc_cursor.lastrowid)
                    else:
                        document_id = int(existing["id"])
                        connection.execute(
                            """
                            UPDATE knowledge_documents
                            SET source_type = ?, title = ?, content = ?, extra_metadata_json = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                source_type,
                                title,
                                content,
                                json.dumps({"path": source_ref}),
                                now,
                                document_id,
                            ),
                        )
                        connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))

                    for index, chunk in enumerate(self._chunk_text(content)):
                        connection.execute(
                            """
                            INSERT INTO knowledge_chunks(document_id, chunk_index, content, search_text)
                            VALUES (?, ?, ?, ?)
                            """,
                            (document_id, index, chunk, chunk.lower()),
                        )
                    indexed += 1

                connection.execute(
                    """
                    UPDATE ingestion_runs
                    SET status = ?, documents_indexed = ?, message = ?
                    WHERE id = ?
                    """,
                    ("completed", indexed, f"Indexed {indexed} document(s)", run_id),
                )
                connection.commit()
            except Exception as exc:
                connection.execute(
                    "UPDATE ingestion_runs SET status = ?, message = ? WHERE id = ?",
                    ("failed", str(exc), run_id),
                )
                connection.commit()
                raise
        return indexed

    def search(self, query: str, limit: int = 10) -> list[KnowledgeHit]:
        tokens = [token.strip().lower() for token in re.split(r"\W+", query) if token.strip()]
        with self._session_factory() as connection:
            rows = connection.execute(
                """
                SELECT
                    kc.id,
                    kc.document_id,
                    kc.content,
                    kc.search_text,
                    kd.source_type,
                    kd.source_ref,
                    kd.title
                FROM knowledge_chunks kc
                JOIN knowledge_documents kd ON kd.id = kc.document_id
                """
            ).fetchall()

        scored: list[KnowledgeHit] = []
        for row in rows:
            score = sum(row["search_text"].count(token) for token in tokens) if tokens else 0
            if tokens and score == 0:
                continue
            scored.append(
                KnowledgeHit(
                    id=int(row["id"]),
                    document_id=int(row["document_id"]),
                    source_type=row["source_type"],
                    source_ref=row["source_ref"],
                    title=row["title"],
                    snippet=row["content"][:280],
                    score=score or 1,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

    def _iter_files(self, target: Path) -> list[Path]:
        if target.is_file():
            return [target]
        if not target.exists():
            return []
        allowed_suffixes = {".md", ".txt", ".yaml", ".yml", ".json"}
        return [path for path in target.rglob("*") if path.is_file() and path.suffix.lower() in allowed_suffixes]

    def _read_file(self, path: Path) -> str:
        suffix = path.suffix.lower()
        text = path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".json":
            try:
                parsed = json.loads(text)
                return json.dumps(parsed, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                return text
        return text

    def _chunk_text(self, content: str, chunk_size: int = 700) -> list[str]:
        normalized = content.strip()
        if not normalized:
            return []
        return [normalized[index : index + chunk_size] for index in range(0, len(normalized), chunk_size)]

    def _source_type_for(self, path: Path) -> str:
        lowered = str(path).lower()
        if "home assistant" in lowered or path.name in {"configuration.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml"}:
            return "ha_config"
        if path.suffix.lower() == ".json" and "entity" in path.name.lower():
            return "ha_entities"
        if "faq" in path.name.lower() or "rule" in path.name.lower():
            return "house_rule"
        if "manual" in path.name.lower():
            return "device_manual"
        if "playbook" in path.name.lower():
            return "playbook"
        return "user_note"

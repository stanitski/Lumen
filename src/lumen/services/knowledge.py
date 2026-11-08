"""Knowledge ingestion service that indexes uploaded text into long-term memory."""

from __future__ import annotations

import asyncio
import logging
import math
import re

from fastapi import HTTPException, UploadFile

from lumen.config import DEFAULT_LUMEN_USER_ID, Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.models import KnowledgeScope, KnowledgeUploadResult
from lumen.services.file_text import FileTextExtractionError, FileTextExtractor


class KnowledgeService:
    _KNOWLEDGE_SOURCE_PREFIX = "knowledge"

    def __init__(
        self,
        store: MemoryStore,
        ollama: OllamaConnector,
        settings: Settings,
        *,
        default_user_id: str = DEFAULT_LUMEN_USER_ID,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._ollama = ollama
        self._settings = settings
        self._default_user_id = default_user_id
        self._logger = logger or logging.getLogger("lumen")

    def _knowledge_user_id(self, scope: KnowledgeScope, user_id: str | None) -> str:
        if scope == "global":
            return "global"
        effective_user_id = (user_id or "").strip()
        return effective_user_id or self._default_user_id

    @staticmethod
    def _normalize_source_ref(value: str | None, fallback: str) -> str:
        candidate = (value or fallback).strip()
        candidate = re.sub(r"[^\w.\-:]+", "_", candidate, flags=re.UNICODE)
        candidate = candidate.strip("._-:")
        return candidate or fallback

    @classmethod
    def _knowledge_source_ref(cls, *, kind: str, scope: KnowledgeScope, source_ref: str) -> str:
        return f"{cls._KNOWLEDGE_SOURCE_PREFIX}:{scope}:{kind}:{source_ref}"

    def _infer_knowledge_kind(self, filename: str | None, content_type: str | None) -> str:
        return FileTextExtractor.infer_kind(filename, content_type)

    @staticmethod
    def _chunk_text(text: str, max_chunk_chars: int = 8000) -> list[str]:
        normalized = re.sub(r"\r\n?", "\n", text).strip()
        if not normalized:
            return []

        max_chunk_chars = max(500, int(max_chunk_chars))
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
        chunks: list[str] = []
        current = ""

        def flush_current() -> None:
            nonlocal current
            chunk = current.strip()
            if chunk:
                chunks.append(chunk)
            current = ""

        for paragraph in paragraphs:
            if len(paragraph) > max_chunk_chars:
                flush_current()
                start = 0
                while start < len(paragraph):
                    end = min(len(paragraph), start + max_chunk_chars)
                    piece = paragraph[start:end].strip()
                    if piece:
                        chunks.append(piece)
                    if end >= len(paragraph):
                        break
                    start = end
                continue

            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if current and len(candidate) > max_chunk_chars:
                flush_current()
                current = paragraph
            else:
                current = candidate

        flush_current()
        return chunks

    async def _index_text_knowledge(
        self,
        *,
        raw: bytes,
        filename: str,
        content_type: str | None,
        ingest_user_id: str,
        source_ref: str,
        max_chunk_chars: int,
        scope: KnowledgeScope,
        kind: str = "text",
    ) -> KnowledgeUploadResult:
        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        try:
            _, text = await asyncio.to_thread(
                FileTextExtractor.extract_text, filename=filename, content_type=content_type, raw=raw
            )
        except FileTextExtractionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        chunks = self._chunk_text(text, max_chunk_chars=max_chunk_chars)
        if not chunks:
            raise HTTPException(status_code=400, detail="No indexable text was found in the uploaded file.")

        embedding_payload = await self._ollama.embed(input=chunks, model=self._settings.ollama_embedding_model)
        embeddings = embedding_payload.get("embeddings") if isinstance(embedding_payload, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(chunks):
            raise RuntimeError("Ollama returned an unexpected embeddings payload")

        prepared: list[tuple[str, list[float], dict[str, object]]] = []
        dimensions: int | None = None
        safe_filename = filename or "upload"
        for chunk_index, (chunk, vector) in enumerate(zip(chunks, embeddings)):
            if not isinstance(vector, list) or not vector:
                raise RuntimeError(f"Invalid embedding vector for chunk {chunk_index}")
            try:
                values = [float(value) for value in vector]
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError(f"Invalid embedding vector for chunk {chunk_index}") from exc
            if not all(math.isfinite(value) for value in values) or not any(values):
                raise RuntimeError(f"Invalid embedding vector for chunk {chunk_index}")
            if dimensions is not None and len(values) != dimensions:
                raise RuntimeError(f"Inconsistent embedding dimensions for chunk {chunk_index}")
            dimensions = len(values)
            prepared.append((
                chunk,
                values,
                {
                    "kind": "text",
                    "document_kind": kind,
                    "filename": safe_filename,
                    "content_type": content_type,
                    "source_ref": source_ref,
                    "chunk_index": chunk_index,
                    "chunk_count": len(chunks),
                    "max_chunk_chars": max_chunk_chars,
                },
            ))

        embeddings_created = await asyncio.to_thread(
            self._store.replace_embeddings,
            user_id=ingest_user_id,
            source_type=f"knowledge_{kind}_{scope}",
            source_ref=self._knowledge_source_ref(kind=kind, scope=scope, source_ref=source_ref),
            chunks=prepared,
            importance=6,
        )

        return KnowledgeUploadResult(
            status="indexed",
            kind=kind,  # type: ignore[arg-type]
            scope=scope,
            user_id=ingest_user_id,
            filename=safe_filename,
            content_type=content_type or "application/octet-stream",
            source_ref=source_ref,
            chunks_created=len(chunks),
            embeddings_created=embeddings_created,
            message="Добре, все запам'ятав.",
        )

    @staticmethod
    def _pending_knowledge_response(
        *,
        kind: str,
        ingest_user_id: str,
        filename: str,
        content_type: str,
        source_ref: str,
        scope: KnowledgeScope,
    ) -> KnowledgeUploadResult:
        messages = {
            "image": "Image upload received, but OCR / vision extraction is not implemented yet.",
            "video": "Video upload received, but video frame/audio extraction is not implemented yet.",
        }
        return KnowledgeUploadResult(
            status="pending",
            kind=kind,  # type: ignore[arg-type]
            scope=scope,
            user_id=ingest_user_id,
            filename=filename,
            content_type=content_type,
            source_ref=source_ref,
            chunks_created=0,
            embeddings_created=0,
            message=messages.get(kind, "Knowledge ingestion is pending for this file type."),
        )

    async def upload(
        self,
        *,
        file: UploadFile,
        scope: KnowledgeScope,
        ingest_user_id: str | None,
        source_ref: str | None,
        max_chunk_chars: int,
    ) -> KnowledgeUploadResult:
        raw = await file.read()
        return await self.upload_bytes(
            filename=file.filename or "upload",
            content_type=file.content_type,
            raw=raw,
            scope=scope,
            ingest_user_id=ingest_user_id,
            source_ref=source_ref,
            max_chunk_chars=max_chunk_chars,
        )

    async def upload_bytes(
        self,
        *,
        filename: str,
        content_type: str | None,
        raw: bytes,
        scope: KnowledgeScope,
        ingest_user_id: str | None,
        source_ref: str | None,
        max_chunk_chars: int,
    ) -> KnowledgeUploadResult:
        effective_user_id = self._knowledge_user_id(scope, ingest_user_id)
        effective_source_ref = self._normalize_source_ref(source_ref, filename or "knowledge-upload")
        kind = self._infer_knowledge_kind(filename, content_type)

        if kind in {"text", "pdf", "docx", "xlsx", "pptx"}:
            return await self._index_text_knowledge(
                raw=raw,
                filename=filename,
                content_type=content_type,
                ingest_user_id=effective_user_id,
                source_ref=effective_source_ref,
                max_chunk_chars=max_chunk_chars,
                scope=scope,
                kind=kind,
            )
        if kind in {"image", "video"}:
            return self._pending_knowledge_response(
                kind=kind,
                ingest_user_id=effective_user_id,
                filename=filename or "upload",
                content_type=content_type or "application/octet-stream",
                source_ref=effective_source_ref,
                scope=scope,
            )
        return KnowledgeUploadResult(
            status="unsupported",
            kind="unsupported",
            scope=scope,
            user_id=effective_user_id,
            filename=filename or "upload",
            content_type=content_type or "application/octet-stream",
            source_ref=effective_source_ref,
            chunks_created=0,
            embeddings_created=0,
            message="Unsupported file type for knowledge ingestion.",
        )

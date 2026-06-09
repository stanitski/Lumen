from __future__ import annotations

import json
import logging
import re
from pathlib import Path
import sys
from typing import get_args

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile

# If this file is started directly from an IDE, add `src` to imports.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.models import (
    ChatRequest,
    ChatResponse,
    KnowledgeUploadResult,
    MemoryFact,
    MemoryPredicate,
    SleepResult,
    TelegramSendRequest,
    TelegramSendResult,
)
from lumen.storage.db import Database


# The app is assembled once at startup and reused by every request.
app = FastAPI(title="Lumen Learning API", version="0.1.0")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lumen")
# Settings define the DB path, Ollama URL, model name, and server host/port.
settings = Settings()
# SQLite database used for conversation history and long-term memory facts.
database = Database(settings.lumen_db_path)
database.init()
# Helper object that knows how to insert and query memory facts.
memory_store = MemoryStore(database.session)
# Async client wrapper around Ollama's HTTP API.
ollama = OllamaConnector(
    base_url=settings.ollama_base_url,
    model=settings.ollama_model,
    timeout_seconds=settings.ollama_timeout_seconds,
    keep_alive=settings.ollama_keep_alive,
)

conversation_id = "lumen-conversation"
user_id = "lumen-user"

ALLOWED_PREDICATES = set(get_args(MemoryPredicate))
TEXT_KNOWLEDGE_EXTENSIONS = {
    ".csv",
    ".htm",
    ".html",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".markdown",
    ".rst",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@app.middleware("http")
async def log_chat_requests(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/chat":
        body = await request.body()
        logger.info("Incoming /chat body: %s", body.decode("utf-8", errors="replace"))
    return await call_next(request)


# Build a stable per-user context namespace so different users do not share memory.
def _resolve_chat_context(payload: ChatRequest) -> tuple[str, str]:
    effective_user_id = (payload.user_id or user_id).strip() or user_id
    base_conversation_id = (payload.conversation_id or conversation_id).strip() or conversation_id
    effective_conversation_id = f"{base_conversation_id}:{effective_user_id}"
    return effective_conversation_id, effective_user_id


# Ollama can sometimes return JSON wrapped in markdown fences, so we strip those
# and parse the payload defensively.
def _safe_json_loads(content: str) -> dict[str, object] | None:
    if not content:
        return None
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# Normalize and validate the model's memory facts before writing them to SQLite.
def _normalize_memory_facts(raw_facts: object, user_id: str) -> list[dict[str, object]]:
    if not isinstance(raw_facts, list):
        return []

    allowed_categories = {
        "preference",
        "profile",
        "routine",
        "rule",
        "relationship",
        "project",
        "secret",
        "credential",
        "device_alias",
    }
    results: list[dict[str, object]] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().lower()
        raw_subject = str(item.get("subject", "")).strip()
        predicate = str(item.get("predicate", "")).strip()
        value = str(item.get("value", "")).strip().rstrip(".")
        if category not in allowed_categories or not predicate or len(value) < 2:
            continue
        normalized_predicate = str(predicate).strip().lower().replace(" ", "_")
        if normalized_predicate not in ALLOWED_PREDICATES:
            continue
        subject = "current_user" if category == "profile" else raw_subject
        if category != "profile" and not subject:
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
                "user_id": user_id,
                "subject": subject[:120],
                "predicate": predicate[:120],
                "value": value[:500],
                "confidence": min(max(confidence, 0.0), 1.0),
                "importance": min(max(importance, 1), 10),
                "tags": tags[:8],
            }
        )
    return results


# Persist the validated facts into the long-term memory table.
def _store_memory_facts(facts: list[dict[str, object]], source_ref: str, user_id: str) -> None:
    for fact in facts:
        fact_copy = dict(fact)
        fact_user_id = str(fact_copy.pop("user_id", user_id)).strip() or user_id
        memory_store.add_fact(user_id=fact_user_id, source_ref=source_ref, **fact_copy)


# Render facts into a compact prompt block for retrieval context.
def _format_memory_facts_for_prompt(facts) -> str:
    if not facts:
        return "No relevant long-term facts were found."
    lines = [
        f"- [{fact.category}] {fact.subject} {fact.predicate} {fact.value} "
        f"(confidence={fact.confidence:.2f}, importance={fact.importance}, source={fact.source_ref})"
        for fact in facts
    ]
    return "Relevant long-term facts:\n" + "\n".join(lines)


# Convert one finished dialogue turn into a single embedding-ready text block.
def _turn_embedding_text(turn) -> str:
    return f"Question:\n{turn.question}\n\nAnswer:\n{turn.answer}"


# Render retrieved memory embeddings into a compact prompt block.
def _format_embedding_hits_for_prompt(hits) -> str:
    if not hits:
        return "No relevant long-term dialogue memories were found."
    lines = [
        f"- score={hit.similarity:.3f}, source={hit.source_ref}, importance={hit.importance}\n"
        f"  {hit.content}"
        for hit in hits
    ]
    return "Relevant long-term dialogue memories:\n" + "\n".join(lines)


def _format_knowledge_hits_for_prompt(hits) -> str:
    if not hits:
        return "No relevant knowledge documents were found."
    lines = [
        f"- score={hit.similarity:.3f}, source={hit.source_ref}, chunk={hit.chunk_index}, importance={hit.importance}\n"
        f"  {hit.content}"
        for hit in hits
    ]
    return "Relevant knowledge documents:\n" + "\n".join(lines)


def _normalize_source_ref(value: str | None, fallback: str) -> str:
    candidate = (value or fallback).strip()
    candidate = re.sub(r"[^\w.\-:]+", "_", candidate, flags=re.UNICODE)
    candidate = candidate.strip("._-:")
    return candidate or fallback


def _infer_knowledge_kind(filename: str | None, content_type: str | None) -> str:
    safe_filename = (filename or "").strip().lower()
    safe_content_type = (content_type or "").strip().lower()
    suffix = Path(safe_filename).suffix.lower()
    if safe_content_type.startswith("text/") or suffix in TEXT_KNOWLEDGE_EXTENSIONS:
        return "text"
    if safe_content_type == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if safe_content_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        return "image"
    if safe_content_type.startswith("video/") or suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}:
        return "video"
    return "unsupported"


def _decode_upload_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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
    *,
    file: UploadFile,
    ingest_user_id: str,
    source_ref: str,
    max_chunk_chars: int,
) -> KnowledgeUploadResult:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    text = _decode_upload_bytes(raw)
    chunks = _chunk_text(text, max_chunk_chars=max_chunk_chars)
    if not chunks:
        raise HTTPException(status_code=400, detail="No indexable text was found in the uploaded file.")

    deleted_embeddings = memory_store.delete_embeddings(
        user_id=ingest_user_id,
        source_type="knowledge_text",
        source_ref=source_ref,
    )

    embedding_payload = await ollama.embed(input=chunks, model=settings.ollama_embedding_model)
    embeddings = embedding_payload.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(chunks):
        raise RuntimeError("Ollama returned an unexpected embeddings payload")

    embeddings_created = 0
    safe_filename = file.filename or "upload"
    for chunk_index, (chunk, vector) in enumerate(zip(chunks, embeddings)):
        if not isinstance(vector, list) or not vector:
            raise RuntimeError(f"Invalid embedding vector for chunk {chunk_index}")
        memory_store.add_embedding(
            user_id=ingest_user_id,
            source_type="knowledge_text",
            source_ref=source_ref,
            chunk_index=chunk_index,
            role=None,
            content=chunk,
            embedding=[float(value) for value in vector],
            embedding_model=settings.ollama_embedding_model,
            importance=6,
            metadata={
                "kind": "text",
                "filename": safe_filename,
                "content_type": file.content_type,
                "source_ref": source_ref,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
                "max_chunk_chars": max_chunk_chars,
            },
        )
        embeddings_created += 1

    return KnowledgeUploadResult(
        status="indexed",
        kind="text",
        user_id=ingest_user_id,
        filename=safe_filename,
        content_type=file.content_type or "application/octet-stream",
        source_ref=source_ref,
        chunks_created=len(chunks),
        embeddings_created=embeddings_created,
        message=(
            f"Text file indexed into long-term memory. Replaced {deleted_embeddings} existing embeddings."
        ),
    )


def _pending_knowledge_response(
    *,
    kind: str,
    ingest_user_id: str,
    filename: str,
    content_type: str,
    source_ref: str,
) -> KnowledgeUploadResult:
    messages = {
        "pdf": "PDF upload received, but PDF text extraction is not implemented yet.",
        "image": "Image upload received, but OCR / vision extraction is not implemented yet.",
        "video": "Video upload received, but video frame/audio extraction is not implemented yet.",
    }
    return KnowledgeUploadResult(
        status="pending",
        kind=kind,  # type: ignore[arg-type]
        user_id=ingest_user_id,
        filename=filename,
        content_type=content_type,
        source_ref=source_ref,
        chunks_created=0,
        embeddings_created=0,
        message=messages.get(kind, "Knowledge ingestion is pending for this file type."),
    )


async def _send_telegram_message(payload: TelegramSendRequest) -> TelegramSendResult:
    if not settings.home_assistant_token.strip():
        raise HTTPException(status_code=503, detail="Home Assistant token is not configured.")

    service_url = settings.home_assistant_url.rstrip("/") + "/api/services/telegram_bot/send_message"
    headers = {
        "Authorization": f"Bearer {settings.home_assistant_token.strip()}",
        "Content-Type": "application/json",
    }
    body: dict[str, object] = {
        "chat_id": payload.chat_id,
        "message": payload.message,
    }
    if payload.parse_mode:
        body["parse_mode"] = payload.parse_mode

    async with httpx.AsyncClient(timeout=settings.ollama_timeout_seconds) as client:
        response = await client.post(service_url, headers=headers, json=body)

    try:
        response_body = response.json()
    except json.JSONDecodeError:
        response_body = {"raw": response.text}

    if response.is_success:
        return TelegramSendResult(
            ok=True,
            status_code=response.status_code,
            response=response_body if isinstance(response_body, dict) else {"raw": response.text},
            message="Telegram message sent through Home Assistant.",
        )

    raise HTTPException(
        status_code=response.status_code,
        detail={
            "message": "Home Assistant rejected the Telegram send request.",
            "response": response_body,
        },
    )


# Build the prompt, call Ollama once, and expect both an answer and memory facts.
async def _answer_with_memory(payload_text: str, conversation_scope_id: str, user_id: str) -> ChatResponse:
    recent_turns = memory_store.recent_turns(conversation_scope_id, limit=50, hours=24)
    relevant_facts = memory_store.search(payload_text, user_id=user_id, limit=10)
    query_embedding_payload = await ollama.embed(input=payload_text, model=settings.ollama_embedding_model)
    query_embeddings = query_embedding_payload.get("embeddings")
    if not isinstance(query_embeddings, list) or not query_embeddings:
        raise RuntimeError("Ollama did not return query embeddings")
    query_vector = query_embeddings[0]
    if not isinstance(query_vector, list) or not query_vector:
        raise RuntimeError("Invalid query embedding vector")
    relevant_turns = memory_store.search_embeddings(
        [float(value) for value in query_vector],
        user_id=user_id,
        limit=5,
        source_types=["dialogue_turn"],
    )
    relevant_knowledge = memory_store.search_embeddings(
        [float(value) for value in query_vector],
        user_id=user_id,
        limit=5,
        source_types=["knowledge_text"],
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful assistant. Use the recent conversation history as context. "
                "Return ONLY valid JSON with keys: answer, memory_facts.\n"
                "Schema:\n"
                "{\n"
                '  "answer": "string",\n'
                '  "memory_facts": [\n'
                "    {\n"
                '      "category": "preference|profile|routine|rule|relationship|project|secret|credential|device_alias",\n'
                '      "subject": "string",\n'
                '      "predicate": "string",\n'
                '      "value": "string",\n'
                '      "confidence": 0.0,\n'
                '      "importance": 1,\n'
                '      "tags": ["string"]\n'
                "    }\n"
                "  ]\n"
                "}\n"
                "If the user message contains no durable facts, set memory_facts to [].\n"
                "For profile facts, use subject='current_user' instead of repeating the person's name.\n"
                "Example: {\"category\":\"profile\",\"subject\":\"current_user\",\"predicate\":\"preferred_name\",\"value\":\"Vlad\"}.\n"
                "Allowed predicate values are: is, prefers, likes, dislikes, alias, uses, owns, works_at, lives_in, role, preferred_name.\n"
                "Use predicate='is' for identity-style facts, like names or descriptions, when appropriate.\n"
                "confidence must be a number from 0.0 to 1.0, where 0.0 means a guess and 1.0 means very sure.\n"
                "importance must be an integer from 1 to 10, where 10 is most important.\n"
                "Return answer as plain text only.\n"
                "Do not use Markdown, HTML, headings, bullets, code fences, or decorative formatting in the answer.\n"
                "Keep the answer concise and natural.\n"
                "Do not include markdown fences or explanations."
            ),
        },
        {
            "role": "system",
            "content": _format_memory_facts_for_prompt(relevant_facts),
        },
        {
            "role": "system",
            "content": _format_embedding_hits_for_prompt(relevant_turns),
        },
        {
            "role": "system",
            "content": _format_knowledge_hits_for_prompt(relevant_knowledge),
        },
        {
            "role": "system",
            "content": "Recent conversation from the last 24 hours:\n"
            + "\n".join(f"- [{item.created_at}] Q: {item.question}\n  A: {item.answer}" for item in recent_turns)
            if recent_turns
            else "No recent conversation history is available.",
        },
        {"role": "user", "content": payload_text},
    ]

    payload = await ollama.chat(messages=messages)
    content = str(payload.get("message", {}).get("content", "")).strip()
    raw = _safe_json_loads(content)
    if raw is None:
        return ChatResponse(answer=content or "I could not parse the model response.")

    answer = str(raw.get("answer", "")).strip() or "I did not receive an answer."
    memory_facts = _normalize_memory_facts(raw.get("memory_facts", []), user_id)
    return ChatResponse(answer=answer, memory_facts=[MemoryFact(**fact) for fact in memory_facts])


# Nightly memory cycle:
# 1. find finished turns older than 24 hours
# 2. embed each turn as question+answer
# 3. store the embedding
# 4. delete the raw turn only after successful storage
async def _sleep_memory(hours: int = 24) -> SleepResult:
    processed_turns = 0
    deleted_turns = 0
    old_turns = memory_store.old_turns(hours=hours, limit=1000)
    for turn in old_turns:
        embedding_text = _turn_embedding_text(turn)
        embedding_payload = await ollama.embed(input=embedding_text, model=settings.ollama_embedding_model)
        embeddings = embedding_payload.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError(f"Ollama did not return embeddings for turn {turn.id}")
        vector = embeddings[0]
        if not isinstance(vector, list) or not vector:
            raise RuntimeError(f"Invalid embedding vector for turn {turn.id}")
        memory_store.add_embedding(
            user_id=turn.user_id,
            source_type="dialogue_turn",
            source_ref=f"turn:{turn.id}",
            chunk_index=0,
            role="turn",
            content=embedding_text,
            embedding=[float(value) for value in vector],
            embedding_model=settings.ollama_embedding_model,
            importance=5,
            metadata={
                "conversation_id": turn.conversation_id,
                "user_id": turn.user_id,
                "question_created_at": turn.question_created_at,
                "answer_created_at": turn.answer_created_at,
                "created_at": turn.created_at,
            },
        )
        processed_turns += 1
        if memory_store.delete_turn(turn.id):
            deleted_turns += 1
    return SleepResult(processed_turns=processed_turns, deleted_turns=deleted_turns)


# Basic endpoint to verify the app is running.
@app.get("/")
async def home() -> dict[str, str]:
    return {"message": "FastAPI is running"}


# Main chat endpoint:
# 1. ask Ollama for an answer and any facts worth saving
# 2. store the completed turn as one record
# 3. store the returned facts
@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    effective_conversation_id, effective_user_id = _resolve_chat_context(payload)
    response = await _answer_with_memory(payload.text, effective_conversation_id, effective_user_id)
    turn_id = memory_store.add_turn(
        conversation_id=effective_conversation_id,
        user_id=effective_user_id,
        source="learning-api",
        question=payload.text,
        answer=response.answer,
    )
    if response.memory_facts:
        _store_memory_facts([fact.model_dump() for fact in response.memory_facts], source_ref=f"turn:{turn_id}", user_id=effective_user_id)
    return response


# Manually trigger the nightly memory cycle.
@app.post("/sleep", response_model=SleepResult)
async def sleep() -> SleepResult:
    return await _sleep_memory(hours=24)


# Upload a knowledge file, index text immediately, and leave non-text stubs explicit.
@app.post("/knowledge/upload", response_model=KnowledgeUploadResult)
async def upload_knowledge(
    file: UploadFile = File(...),
    ingest_user_id: str = Form(default=user_id),
    source_ref: str | None = Form(default=None),
    max_chunk_chars: int = Form(default=8000),
) -> KnowledgeUploadResult:
    effective_user_id = (ingest_user_id or user_id).strip() or user_id
    effective_source_ref = _normalize_source_ref(source_ref, file.filename or "knowledge-upload")
    kind = _infer_knowledge_kind(file.filename, file.content_type)

    if kind == "text":
        return await _index_text_knowledge(
            file=file,
            ingest_user_id=effective_user_id,
            source_ref=effective_source_ref,
            max_chunk_chars=max_chunk_chars,
        )
    if kind in {"pdf", "image", "video"}:
        return _pending_knowledge_response(
            kind=kind,
            ingest_user_id=effective_user_id,
            filename=file.filename or "upload",
            content_type=file.content_type or "application/octet-stream",
            source_ref=effective_source_ref,
        )
    return KnowledgeUploadResult(
        status="unsupported",
        kind="unsupported",
        user_id=effective_user_id,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        source_ref=effective_source_ref,
        chunks_created=0,
        embeddings_created=0,
        message="Unsupported file type for knowledge ingestion.",
    )


# Send a Telegram message through Home Assistant directly.
@app.post("/telegram/send", response_model=TelegramSendResult)
async def telegram_send(payload: TelegramSendRequest) -> TelegramSendResult:
    return await _send_telegram_message(payload)


# Direct execution path for IDE debugging.
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.lumen_host, port=settings.lumen_port)

from __future__ import annotations

import json
import logging
import re
import asyncio
from pathlib import Path
import sys
from typing import get_args
from uuid import uuid4

from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile

# If this file is started directly from an IDE, add `src` to imports.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.models import (
    ActionExecutionResult,
    ChatRequest,
    ChatResponse,
    KnowledgeScope,
    KnowledgeUploadRequest,
    KnowledgeUploadResult,
    MemoryFact,
    MemoryPredicate,
    PlannerAction,
    PlannerResponse,
    SleepResult,
    TalkSendRequest,
    TalkSendResult,
    TelegramSendRequest,
    TelegramSendResult,
    ReminderActionPayload,
)
from lumen.storage.db import Database
from lumen.time_utils import LOCAL_TIMEZONE, local_now, local_now_iso


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
app.state.reminder_scheduler_stop = None
app.state.reminder_scheduler_task = None

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
REMINDER_POLL_INTERVAL_SECONDS = 30


@app.middleware("http")
# Log incoming chat requests before FastAPI validation runs.
async def log_chat_requests(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/chat":
        body = await request.body()
        logger.info("Incoming /chat body: %s", body.decode("utf-8", errors="replace"))
    return await call_next(request)


# Start the reminder scheduler once the app is ready.
@app.on_event("startup")
async def _startup_background_tasks() -> None:
    if app.state.reminder_scheduler_task is None or app.state.reminder_scheduler_task.done():
        app.state.reminder_scheduler_stop = asyncio.Event()
        app.state.reminder_scheduler_task = asyncio.create_task(_reminder_scheduler_loop())


# Stop the reminder scheduler cleanly during shutdown.
@app.on_event("shutdown")
async def _shutdown_background_tasks() -> None:
    stop_event = app.state.reminder_scheduler_stop
    if stop_event is not None:
        stop_event.set()
    task = app.state.reminder_scheduler_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# Build a stable per-user context namespace so different users do not share memory.
# Resolve the conversation and user scope for the current request.
def _resolve_chat_context(payload: ChatRequest) -> tuple[str, str]:
    effective_user_id = (payload.user_id or user_id).strip() or user_id
    base_conversation_id = (payload.conversation_id or conversation_id).strip() or conversation_id
    effective_conversation_id = f"{base_conversation_id}:{effective_user_id}"
    return effective_conversation_id, effective_user_id


# Ollama can sometimes return JSON wrapped in markdown fences, so we strip those
# and parse the payload defensively.
# Parse a JSON payload from model output with basic fence cleanup.
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
# Validate and normalize fact payloads before persistence.
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
# Store normalized facts in the database.
def _store_memory_facts(facts: list[dict[str, object]], source_ref: str, user_id: str) -> None:
    for fact in facts:
        fact_copy = dict(fact)
        fact_user_id = str(fact_copy.pop("user_id", user_id)).strip() or user_id
        memory_store.add_fact(user_id=fact_user_id, source_ref=source_ref, **fact_copy)


# Render facts into a compact prompt block for retrieval context.
# Turn long-term facts into a readable prompt block.
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
# Turn a completed dialogue pair into embedding text.
def _turn_embedding_text(turn) -> str:
    return f"Question:\n{turn.question}\n\nAnswer:\n{turn.answer}"


# Render retrieved memory embeddings into a compact prompt block.
# Format retrieved dialogue embeddings for prompt context.
def _format_embedding_hits_for_prompt(hits) -> str:
    if not hits:
        return "No relevant long-term dialogue memories were found."
    lines = [
        f"- score={hit.similarity:.3f}, source={hit.source_ref}, importance={hit.importance}\n"
        f"  {hit.content}"
        for hit in hits
    ]
    return "Relevant long-term dialogue memories:\n" + "\n".join(lines)


# Format retrieved knowledge chunks for prompt context.
def _format_knowledge_hits_for_prompt(hits) -> str:
    if not hits:
        return "No relevant knowledge documents were found."
    lines = [
        f"- score={hit.similarity:.3f}, source={hit.source_ref}, chunk={hit.chunk_index}, importance={hit.importance}\n"
        f"  {hit.content}"
        for hit in hits
    ]
    return "Relevant knowledge documents:\n" + "\n".join(lines)


# Format planned actions for planner/final-answer prompts.
def _format_planner_actions_for_prompt(actions: list[PlannerAction]) -> str:
    if not actions:
        return "No actions were planned."
    lines: list[str] = []
    for action in actions:
        payload_json = json.dumps(action.payload, ensure_ascii=False, indent=2, sort_keys=True)
        lines.append(f"- type={action.type}\n  payload={payload_json}")
    return "Planned actions:\n" + "\n".join(lines)


# Format action execution results for the final-answer prompt.
def _format_action_results_for_prompt(results: list[ActionExecutionResult]) -> str:
    if not results:
        return "No actions were executed."
    lines: list[str] = []
    for result in results:
        details_json = json.dumps(result.details, ensure_ascii=False, indent=2, sort_keys=True)
        lines.append(f"- type={result.type}, ok={result.ok}\n  {result.message}\n  details={details_json}")
    return "Executed actions:\n" + "\n".join(lines)


# Parse an ISO datetime string and normalize it to an aware timestamp.
def _normalize_iso_datetime_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.isoformat()


# Detect timer-like phrasing so Planner can treat it as a reminder even when the user says "таймер".
def _timer_hint_for_prompt(payload_text: str) -> str:
    normalized = payload_text.strip().lower()
    timer_keywords = ("таймер", "timer", "countdown", "засіч", "засечь")
    if not any(keyword in normalized for keyword in timer_keywords):
        return ""
    return (
        "Timer hint: treat this as a reminder. "
        "If the user provided a relative delay, use duration_seconds. "
        "If the user provided an absolute date/time, use due_at. "
        "Common timer phrases in this request should not be treated as a plain chat response."
    )


# Derive a Telegram chat id from the conversation scope used for reminders.
def _conversation_scope_to_telegram_chat_id(conversation_scope_id: str) -> int | str | None:
    base_scope = conversation_scope_id.rsplit(":", 1)[0] if ":" in conversation_scope_id else conversation_scope_id
    if base_scope.startswith("telegram:"):
        candidate = base_scope.split(":", 1)[1].strip()
        if candidate:
            if candidate.isdigit():
                try:
                    return int(candidate)
                except ValueError:
                    return candidate
            return candidate
    return None


# Resolve which memory user bucket should be used for uploaded knowledge.
def _knowledge_user_id(scope: KnowledgeScope, user_id: str | None) -> str:
    if scope == "global":
        return "global"
    effective_user_id = (user_id or "").strip()
    return effective_user_id or "lumen-user"


# Advance a repeating reminder to its next due_at value.
def _next_reminder_due_at(reminder) -> str | None:
    repeat_interval = reminder.repeat_interval_seconds
    if repeat_interval is None:
        return None
    try:
        current_due = datetime.fromisoformat(reminder.due_at)
    except ValueError:
        return None
    if current_due.tzinfo is None:
        current_due = current_due.replace(tzinfo=LOCAL_TIMEZONE)
    next_due = current_due + timedelta(seconds=int(repeat_interval))
    if reminder.repeat_until:
        try:
            repeat_until = datetime.fromisoformat(reminder.repeat_until)
        except ValueError:
            return None
        if repeat_until.tzinfo is None:
            repeat_until = repeat_until.replace(tzinfo=LOCAL_TIMEZONE)
        if next_due > repeat_until:
            return None
    return next_due.isoformat()


# Turn arbitrary file names into stable source references.
def _normalize_source_ref(value: str | None, fallback: str) -> str:
    candidate = (value or fallback).strip()
    candidate = re.sub(r"[^\w.\-:]+", "_", candidate, flags=re.UNICODE)
    candidate = candidate.strip("._-:")
    return candidate or fallback


# Classify uploaded files into text, pdf, image, video, or unsupported.
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


# Decode uploaded bytes with a few common text encodings.
def _decode_upload_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# Split text into paragraph-first chunks with a safe upper bound.
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


# Read a text upload, chunk it, embed it, and store it in long-term memory.
async def _index_text_knowledge(
    *,
    file: UploadFile,
    ingest_user_id: str,
    source_ref: str,
    max_chunk_chars: int,
    scope: KnowledgeScope,
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
        source_type=f"knowledge_text_{scope}",
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
            source_type=f"knowledge_text_{scope}",
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
        scope=scope,
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


# Return a stub result for upload types we do not extract yet.
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
        "pdf": "PDF upload received, but PDF text extraction is not implemented yet.",
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


# Send a Telegram message through Home Assistant.
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


# Send a plain text message through the Talk-2 Home Assistant script.
async def _send_talk_message(payload: TalkSendRequest) -> TalkSendResult:
    if not settings.home_assistant_token.strip():
        raise HTTPException(status_code=503, detail="Home Assistant token is not configured.")

    service_url = settings.home_assistant_url.rstrip("/") + "/api/services/script/talk_2"
    headers = {
        "Authorization": f"Bearer {settings.home_assistant_token.strip()}",
        "Content-Type": "application/json",
    }
    body: dict[str, object] = {
        "message": payload.message,
    }

    async with httpx.AsyncClient(timeout=settings.ollama_timeout_seconds) as client:
        response = await client.post(service_url, headers=headers, json=body)

    try:
        response_body = response.json()
    except json.JSONDecodeError:
        response_body = {"raw": response.text}

    if response.is_success:
        return TalkSendResult(
            ok=True,
            status_code=response.status_code,
            response=response_body if isinstance(response_body, dict) else {"raw": response.text},
            message="Talk-2 message sent through Home Assistant.",
        )

    raise HTTPException(
        status_code=response.status_code,
        detail={
            "message": "Home Assistant rejected the Talk-2 send request.",
            "response": response_body,
        },
    )


# Mirror a plain-text response to the Talk-2 script without failing the main flow.
async def _mirror_to_talk(message: str) -> None:
    try:
        await _send_talk_message(TalkSendRequest(message=message))
    except Exception as exc:
        logger.exception("Failed to mirror message to Talk-2: %s", exc)


# Build the planner prompt that decides which actions are needed.
def _current_time_context() -> str:
    now = local_now()
    return f"Current date and time: {now.isoformat()}"


# Build the planner prompt that decides which actions are needed.
def _build_planner_messages(
    *,
    payload_text: str,
    conversation_scope_id: str,
    user_id: str,
    timer_hint: str,
    recent_turns,
    relevant_facts,
    relevant_turns,
    relevant_knowledge,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are Lumen Planner. Decide what actions are needed from the user's message.\n"
                "Return ONLY valid JSON with keys: draft_answer, actions.\n"
                "Schema:\n"
                "{\n"
                '  "draft_answer": "string",\n'
                '  "actions": [\n'
                "    {\n"
                '      "type": "memory_fact|reminder|reminder_list|web_search",\n'
                '      "payload": {}\n'
                "    }\n"
                "  ]\n"
                "}\n"
                "Rules:\n"
                "- draft_answer must be plain text only.\n"
                "- Do not use Markdown, HTML, code fences, headings, or bullets in draft_answer.\n"
                "- actions must be as small as possible and only include things that need execution.\n"
                "- Use type=memory_fact for durable facts worth storing.\n"
                "- Use type=reminder for reminders or scheduled follow-ups.\n"
                "- Treat timer/countdown/таймер requests as reminders.\n"
                "- Use type=reminder_list when the user asks what reminders are currently active or scheduled.\n"
                "- Use type=web_search only if external web lookup is explicitly needed.\n"
                "- For reminder payloads, provide text and either due_at in ISO 8601 with timezone or duration_seconds for relative timers.\n"
                "- For recurring reminders, include repeat_interval_seconds and repeat_until when needed.\n"
                "- For reminder_list payloads, include optional status (default pending) and optional limit.\n"
                "- Timer examples: 'постав таймер на 10 хвилин', 'нагадай через 20 хвилин', 'через годину нагадай'.\n"
                "- If the user message contains no actions, return actions=[].\n"
                "- Keep the draft answer concise and natural.\n"
                "- Do not include any explanatory text outside JSON."
            ),
        },
        {
            "role": "system",
            "content": _current_time_context(),
        },
        {
            "role": "system",
            "content": timer_hint or "No timer-specific hint was detected in the user request.",
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
        {
            "role": "system",
            "content": f"Conversation scope: {conversation_scope_id}\nUser: {user_id}",
        },
        {"role": "user", "content": payload_text},
    ]


# Ask Ollama to produce a plan of actions for the current user message.
async def _run_planner(payload_text: str, conversation_scope_id: str, user_id: str) -> PlannerResponse:
    recent_turns = memory_store.recent_turns(conversation_scope_id, limit=50, hours=24)
    relevant_facts = memory_store.search(payload_text, user_id=user_id, limit=10)
    timer_hint = _timer_hint_for_prompt(payload_text)
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
        source_types=["knowledge_text_personal", "knowledge_text_global"],
    )
    payload = await ollama.chat(
        messages=_build_planner_messages(
            payload_text=payload_text,
            conversation_scope_id=conversation_scope_id,
            user_id=user_id,
            timer_hint=timer_hint,
            recent_turns=recent_turns,
            relevant_facts=relevant_facts,
            relevant_turns=relevant_turns,
            relevant_knowledge=relevant_knowledge,
        )
    )
    content = str(payload.get("message", {}).get("content", "")).strip()
    raw = _safe_json_loads(content)
    if raw is None:
        return PlannerResponse(draft_answer=content or "I could not parse the planner response.")

    draft_answer = str(raw.get("draft_answer", raw.get("answer", ""))).strip()
    if not draft_answer:
        draft_answer = "I did not receive a draft answer."

    actions_raw = raw.get("actions")
    actions: list[PlannerAction] = []
    if isinstance(actions_raw, list):
        for item in actions_raw:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("type", "")).strip().lower()
            payload_data = item.get("payload")
            if action_type not in {"memory_fact", "reminder", "reminder_list", "web_search"}:
                continue
            actions.append(
                PlannerAction(
                    type=action_type,  # type: ignore[arg-type]
                    payload=payload_data if isinstance(payload_data, dict) else {},
                )
            )
    else:
        legacy_memory_facts = raw.get("memory_facts")
        if isinstance(legacy_memory_facts, list):
            for item in legacy_memory_facts:
                if isinstance(item, dict):
                    actions.append(PlannerAction(type="memory_fact", payload=item))
        legacy_reminders = raw.get("reminders")
        if isinstance(legacy_reminders, list):
            for item in legacy_reminders:
                if isinstance(item, dict):
                    actions.append(PlannerAction(type="reminder", payload=item))

    return PlannerResponse(draft_answer=draft_answer, actions=actions)


# Normalize a reminder payload and ensure it has valid timestamps.
def _normalize_reminder_payload(
    payload: dict[str, object],
    *,
    user_id: str,
    conversation_id: str,
    source_ref: str,
) -> dict[str, object] | None:
    text = str(payload.get("text", "")).strip()
    due_at = _normalize_iso_datetime_text(payload.get("due_at"))
    duration_seconds_raw = payload.get("duration_seconds")
    duration_seconds: int | None = None
    if due_at is None and duration_seconds_raw is not None:
        try:
            duration_seconds = int(duration_seconds_raw)
        except (TypeError, ValueError):
            return None
        if duration_seconds < 1:
            return None
        due_at = (local_now() + timedelta(seconds=duration_seconds)).isoformat()

    if not text or not due_at:
        return None

    repeat_interval_seconds_raw = payload.get("repeat_interval_seconds")
    repeat_interval_seconds: int | None = None
    if repeat_interval_seconds_raw is not None:
        try:
            repeat_interval_seconds = int(repeat_interval_seconds_raw)
        except (TypeError, ValueError):
            return None
        if repeat_interval_seconds < 1:
            return None

    repeat_until = _normalize_iso_datetime_text(payload.get("repeat_until"))
    try:
        validated = ReminderActionPayload.model_validate(
            {
                "text": text[:500],
                "due_at": due_at,
                "duration_seconds": duration_seconds,
                "repeat_interval_seconds": repeat_interval_seconds,
                "repeat_until": repeat_until,
            }
        )
    except Exception:
        return None
    return {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "source_ref": source_ref,
        **validated.model_dump(exclude={"duration_seconds"}),
    }


# Normalize a reminder list request so the executor can fetch current reminders.
def _normalize_reminder_list_payload(payload: dict[str, object]) -> dict[str, object]:
    status = str(payload.get("status", "pending")).strip().lower() or "pending"
    if status not in {"pending", "sent", "cancelled", "failed"}:
        status = "pending"
    limit_raw = payload.get("limit", 20)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))
    return {"status": status, "limit": limit}


# Normalize a single memory fact payload for the executor.
def _normalize_memory_fact_payload(
    payload: dict[str, object],
    *,
    user_id: str,
) -> list[dict[str, object]]:
    return _normalize_memory_facts([payload], user_id)


# Execute planner actions by routing them to the right persistence layer.
async def _execute_planned_actions(
    actions: list[PlannerAction],
    *,
    user_id: str,
    conversation_scope_id: str,
    source_ref: str,
) -> tuple[list[MemoryFact], list[str], list[ActionExecutionResult]]:
    stored_memory_facts: list[MemoryFact] = []
    created_reminders: list[str] = []
    results: list[ActionExecutionResult] = []

    for action in actions:
        if action.type == "memory_fact":
            normalized_facts = _normalize_memory_fact_payload(action.payload, user_id=user_id)
            if not normalized_facts:
                results.append(
                    ActionExecutionResult(
                        type="memory_fact",
                        ok=False,
                        message="Memory fact payload was invalid.",
                        details={"payload": action.payload},
                    )
                )
                continue
            for fact in normalized_facts:
                memory_store.add_fact(user_id=user_id, source_ref=source_ref, **fact)
                stored_memory_facts.append(MemoryFact(**fact))
            results.append(
                ActionExecutionResult(
                    type="memory_fact",
                    ok=True,
                    message=f"Stored {len(normalized_facts)} memory fact(s).",
                    details={"count": len(normalized_facts)},
                )
            )
            continue

        if action.type == "reminder":
            normalized_reminder = _normalize_reminder_payload(
                action.payload,
                user_id=user_id,
                conversation_id=conversation_scope_id,
                source_ref=source_ref,
            )
            if normalized_reminder is None:
                results.append(
                    ActionExecutionResult(
                        type="reminder",
                        ok=False,
                        message="Reminder payload was invalid.",
                        details={"payload": action.payload},
                    )
                )
                continue
            reminder_id = memory_store.add_reminder(**normalized_reminder)
            created_reminders.append(f"reminder:{reminder_id}")
            results.append(
                ActionExecutionResult(
                    type="reminder",
                    ok=True,
                    message=f"Created reminder #{reminder_id}.",
                    details={"reminder_id": reminder_id, **normalized_reminder},
                )
            )
            continue

        if action.type == "reminder_list":
            normalized_request = _normalize_reminder_list_payload(action.payload)
            reminders = memory_store.list_reminders(
                user_id,
                status=normalized_request["status"],
                limit=normalized_request["limit"],
            )
            reminders_payload = [reminder.model_dump() for reminder in reminders]
            results.append(
                ActionExecutionResult(
                    type="reminder_list",
                    ok=True,
                    message=f"Found {len(reminders_payload)} reminder(s).",
                    details={
                        "status": normalized_request["status"],
                        "limit": normalized_request["limit"],
                        "count": len(reminders_payload),
                        "reminders": reminders_payload,
                    },
                )
            )
            continue

        results.append(
            ActionExecutionResult(
                type="web_search",
                ok=False,
                message="Web search executor is not implemented yet.",
                details={"payload": action.payload},
            )
        )

    return stored_memory_facts, created_reminders, results


# Ask Ollama to turn the plan plus execution results into a user-facing reply.
async def _finalize_chat_answer(
    *,
    payload_text: str,
    planner_draft_answer: str,
    action_results: list[ActionExecutionResult],
    conversation_scope_id: str,
    user_id: str,
) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You are Lumen Final Answer writer.\n"
                "Return only plain text.\n"
                "Do not use Markdown, HTML, bullets, headings, or code fences.\n"
                "Keep the answer concise, natural, and user-facing.\n"
                "Use the planner draft answer and the executed action results to produce the final reply."
            ),
        },
        {
            "role": "system",
            "content": f"Conversation scope: {conversation_scope_id}\nUser: {user_id}",
        },
        {
            "role": "system",
            "content": _current_time_context(),
        },
        {
            "role": "system",
            "content": f"Planner draft answer:\n{planner_draft_answer or 'No draft answer.'}",
        },
        {
            "role": "system",
            "content": _format_action_results_for_prompt(action_results),
        },
        {"role": "user", "content": payload_text},
    ]

    payload = await ollama.chat(messages=messages)
    content = str(payload.get("message", {}).get("content", "")).strip()
    return content or planner_draft_answer or "I did not receive a final answer."


# Nightly memory cycle:
# 1. find finished turns older than 24 hours
# 2. embed each turn as question+answer
# 3. store the embedding
# 4. delete the raw turn only after successful storage
# Sweep old dialogue turns into embeddings and delete the raw turns afterward.
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


# Poll pending reminders and dispatch any due notifications.
async def _run_reminder_dispatch_cycle(limit: int = 100) -> dict[str, int]:
    due_reminders = memory_store.due_reminders(limit=limit)
    dispatched = 0
    rescheduled = 0
    failed = 0

    for reminder in due_reminders:
        chat_id = _conversation_scope_to_telegram_chat_id(reminder.conversation_id or "")
        if chat_id is None:
            memory_store.set_reminder_status(reminder.id, user_id=reminder.user_id, status="failed")
            failed += 1
            continue

        try:
            await _send_telegram_message(
                TelegramSendRequest(
                    chat_id=chat_id,
                    message=reminder.text,
                    parse_mode=None,
                )
            )
            await _mirror_to_talk(reminder.text)
        except Exception as exc:
            logger.exception("Failed to dispatch reminder %s: %s", reminder.id, exc)
            memory_store.set_reminder_status(reminder.id, user_id=reminder.user_id, status="failed")
            failed += 1
            continue

        next_due = _next_reminder_due_at(reminder)
        if next_due is None:
            memory_store.delete_reminder(reminder.id, user_id=reminder.user_id)
            dispatched += 1
            continue

        memory_store.set_reminder_status(
            reminder.id,
            user_id=reminder.user_id,
            status="pending",
            sent_at=local_now_iso(),
            due_at=next_due,
        )
        rescheduled += 1

    return {"dispatched": dispatched, "rescheduled": rescheduled, "failed": failed}


# Background loop that runs reminder dispatch on a fixed interval.
async def _reminder_scheduler_loop() -> None:
    logger.info("Reminder scheduler started with interval=%ss", REMINDER_POLL_INTERVAL_SECONDS)
    stop_event = app.state.reminder_scheduler_stop
    try:
        while not stop_event.is_set():
            try:
                stats = await _run_reminder_dispatch_cycle()
                if any(stats.values()):
                    logger.info(
                        "Reminder cycle finished: dispatched=%s rescheduled=%s failed=%s",
                        stats["dispatched"],
                        stats["rescheduled"],
                        stats["failed"],
                    )
            except Exception as exc:
                logger.exception("Reminder scheduler cycle failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=REMINDER_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Reminder scheduler stopped")


# Basic endpoint to verify the app is running.
@app.get("/")
# Return a tiny health response for browser and local checks.
async def home() -> dict[str, str]:
    return {"message": "FastAPI is running"}


# Main chat endpoint:
# 1. ask Ollama for an answer and any facts worth saving
# 2. store the completed turn as one record
# 3. store the returned facts
@app.post("/chat", response_model=ChatResponse)
# Main chat endpoint: plan actions, execute them, finalize the reply, and store the turn.
async def chat(payload: ChatRequest) -> ChatResponse:
    effective_conversation_id, effective_user_id = _resolve_chat_context(payload)
    interaction_ref = f"interaction:{effective_conversation_id}:{uuid4().hex}"
    planner_response = await _run_planner(payload.text, effective_conversation_id, effective_user_id)
    stored_memory_facts, created_reminders, action_results = await _execute_planned_actions(
        planner_response.actions,
        user_id=effective_user_id,
        conversation_scope_id=effective_conversation_id,
        source_ref=interaction_ref,
    )
    final_answer = await _finalize_chat_answer(
        payload_text=payload.text,
        planner_draft_answer=planner_response.draft_answer,
        action_results=action_results,
        conversation_scope_id=effective_conversation_id,
        user_id=effective_user_id,
    )
    turn_id = memory_store.add_turn(
        conversation_id=effective_conversation_id,
        user_id=effective_user_id,
        source="learning-api",
        question=payload.text,
        answer=final_answer,
    )
    if created_reminders:
        logger.info("Created reminders for %s: %s", effective_user_id, ", ".join(created_reminders))
    if stored_memory_facts:
        logger.info("Stored %s memory facts for turn %s", len(stored_memory_facts), turn_id)
    await _mirror_to_talk(final_answer)
    return ChatResponse(answer=final_answer, memory_facts=stored_memory_facts)


# Manually trigger the nightly memory cycle.
@app.post("/sleep", response_model=SleepResult)
# Manually trigger the nightly memory sweep.
async def sleep() -> SleepResult:
    return await _sleep_memory(hours=24)


# Manually run the reminder dispatch cycle for debugging.
@app.post("/reminders/run")
async def reminders_run() -> dict[str, int]:
    return await _run_reminder_dispatch_cycle()


# Upload a knowledge file, index text immediately, and leave non-text stubs explicit.
@app.post("/knowledge/upload", response_model=KnowledgeUploadResult)
# Upload a knowledge file and index text content into long-term memory.
async def upload_knowledge(
    file: UploadFile = File(...),
    scope: KnowledgeScope = Form(default="personal"),
    ingest_user_id: str | None = Form(default=None),
    source_ref: str | None = Form(default=None),
    max_chunk_chars: int = Form(default=8000),
) -> KnowledgeUploadResult:
    effective_user_id = _knowledge_user_id(scope, ingest_user_id or user_id)
    effective_source_ref = _normalize_source_ref(source_ref, file.filename or "knowledge-upload")
    kind = _infer_knowledge_kind(file.filename, file.content_type)

    if kind == "text":
        return await _index_text_knowledge(
            file=file,
            ingest_user_id=effective_user_id,
            source_ref=effective_source_ref,
            max_chunk_chars=max_chunk_chars,
            scope=scope,
        )
    if kind in {"pdf", "image", "video"}:
        return _pending_knowledge_response(
            kind=kind,
            ingest_user_id=effective_user_id,
            filename=file.filename or "upload",
            content_type=file.content_type or "application/octet-stream",
            source_ref=effective_source_ref,
            scope=scope,
        )
    return KnowledgeUploadResult(
        status="unsupported",
        kind="unsupported",
        scope=scope,
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
# Send a Telegram message through Home Assistant directly.
async def telegram_send(payload: TelegramSendRequest) -> TelegramSendResult:
    return await _send_telegram_message(payload)


# Send a plain text message through the Talk-2 Home Assistant script directly.
@app.post("/talk/send", response_model=TalkSendResult)
async def talk_send(payload: TalkSendRequest) -> TalkSendResult:
    return await _send_talk_message(payload)


# Direct execution path for IDE debugging.
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.lumen_host, port=settings.lumen_port)

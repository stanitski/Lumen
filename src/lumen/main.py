from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import get_args

from fastapi import FastAPI

# If this file is started directly from an IDE, add `src` to imports.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.models import ChatRequest, ChatResponse, MemoryFact, MemoryPredicate, SleepResult
from lumen.storage.db import Database


# The app is assembled once at startup and reused by every request.
app = FastAPI(title="Lumen Learning API", version="0.1.0")
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

# For now this demo uses one fixed conversation/user identity.
conversation_id = "lumen-conversation"
user_id = "lumen-user"

ALLOWED_PREDICATES = set(get_args(MemoryPredicate))


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
        memory_store.add_fact(user_id=user_id, source_ref=source_ref, **fact)


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


# Direct execution path for IDE debugging.
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.lumen_host, port=settings.lumen_port)

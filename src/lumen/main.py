"""HTTP entrypoint for Lumen's chat, memory, and knowledge runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.models import (
    ChatAcceptedResponse,
    ChatRequest,
    ConnectorRequest,
    ConnectorResponse,
    KnowledgeScope,
    KnowledgeUploadResult,
    OllamaCompatibleChatRequest,
    OllamaCompatibleGenerateRequest,
    OllamaCompatibleShowRequest,
)
from lumen.services.chat_only import ChatOnlyService
from lumen.services.context_executor import ContextExecutor
from lumen.services.delivery import DeliveryService
from lumen.services.knowledge import KnowledgeService
from lumen.services.memory import MemoryService
from lumen.services.speech_to_text import SpeechToTextError, SpeechToTextService
from lumen.services.telegram_context import TelegramContextService
from lumen.services.web_search import DuckDuckGoWebSearch
from lumen.storage.db import Database


settings = Settings()


def _configure_logging() -> logging.Logger:
    log_path = Path(settings.log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )
    return logging.getLogger("lumen")


logger = _configure_logging()
database = Database(settings.lumen_db_path)
database.init()
memory_store = MemoryStore(database.session)
ollama = OllamaConnector(
    base_url=settings.ollama_base_url,
    model=settings.ollama_model,
    timeout_seconds=settings.ollama_timeout_seconds,
    keep_alive=settings.ollama_client_keep_alive,
    debug_dump_path=settings.ollama_debug_dump_path,
)
delivery_service = DeliveryService(settings, logger=logger)
memory_service = MemoryService(memory_store, ollama, settings, default_user_id="global", logger=logger)
speech_to_text_service = SpeechToTextService(settings, logger=logger)
telegram_context_service = TelegramContextService(settings, memory_store, speech_to_text_service, logger=logger)
web_search = DuckDuckGoWebSearch(logger=logger)
context_executor = ContextExecutor(telegram_context_service, web_search, logger=logger)
chat_service = ChatOnlyService(
    ollama=ollama,
    memory_service=memory_service,
    delivery_service=delivery_service,
    settings=settings,
    context_executor=context_executor,
    logger=logger,
)
knowledge_service = KnowledgeService(
    memory_store,
    ollama,
    settings,
    default_user_id=settings.lumen_user_id.strip() or "VST",
    logger=logger,
)
app = FastAPI(title="Lumen")


def _user_id(value: object | None) -> str:
    return str(value or settings.lumen_user_id).strip() or "VST"


def _request_id() -> str:
    return uuid4().hex


def _callback_metadata(payload: ConnectorRequest) -> dict[str, object]:
    metadata = dict(payload.reply.metadata) if payload.reply else {}
    metadata.update({"target_channel": payload.source, "target_user_id": _user_id(payload.user_id)})
    if payload.reply and payload.reply.target is not None:
        metadata["target_chat_id"] = str(payload.reply.target)
    return metadata


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict)).strip()
    return ""


def _ollama_model(requested: str) -> str:
    return requested.strip() or settings.ollama_dialogue_model.strip() or settings.ollama_model


def _ollama_stream(payload: dict[str, object]) -> StreamingResponse:
    async def events():
        yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/")
async def home() -> dict[str, str]:
    return {"service": "lumen", "mode": "chat_memory_knowledge", "status": "ok"}


@app.post("/chat", response_model=ChatAcceptedResponse, status_code=202)
async def chat(payload: ChatRequest) -> ChatAcceptedResponse:
    request_id = _request_id()
    asyncio.create_task(chat_service.process_chat_request(payload, request_id))
    return ChatAcceptedResponse(request_id=request_id)


@app.post("/connector", response_model=ConnectorResponse, status_code=202)
async def connector(payload: ConnectorRequest) -> ConnectorResponse:
    request_id = _request_id()
    callback_metadata = _callback_metadata(payload)
    if payload.event_type == "attachment" and payload.file is not None:
        file_name = str(payload.file.name or "voice-message")
        mime_type = str(payload.file.mime_type or "")
        if not speech_to_text_service.is_speech_attachment(file_name=file_name, mime_type=mime_type):
            return ConnectorResponse(request_id=request_id, status="ignored", handled=False, reason="unsupported_attachment")
        try:
            transcript = await speech_to_text_service.transcribe_url(
                file_url=str(payload.file.url or ""), file_name=file_name, mime_type=mime_type
            )
        except SpeechToTextError as exc:
            logger.warning("Speech transcription failed request=%s: %s", request_id, exc)
            return ConnectorResponse(request_id=request_id, status="ignored", handled=False, reason="speech_transcription_failed")
        text = str(payload.text or "").strip()
        user_text = f"{text}\n\n{transcript}" if text else transcript
    elif payload.event_type == "text" and (payload.text or "").strip():
        user_text = str(payload.text)
    else:
        return ConnectorResponse(request_id=request_id, status="ignored", handled=False, reason="unsupported_connector_event")
    asyncio.create_task(
        chat_service.process_chat_request_with_context(
            ChatRequest(text=user_text, user_id=_user_id(payload.user_id), metadata=dict(payload.data)),
            request_id,
            callback_metadata=callback_metadata,
        )
    )
    return ConnectorResponse(request_id=request_id, handled=True)


@app.post("/knowledge/upload", response_model=KnowledgeUploadResult)
async def upload_knowledge(
    file: UploadFile = File(...),
    scope: KnowledgeScope = Form("personal"),
    user_id: str | None = Form(None),
    source_ref: str | None = Form(None),
    max_chunk_chars: int = Form(8000),
) -> KnowledgeUploadResult:
    return await knowledge_service.upload(
        file=file,
        scope=scope,
        ingest_user_id=user_id,
        source_ref=source_ref,
        max_chunk_chars=max_chunk_chars,
    )


@app.get("/memory", response_class=HTMLResponse)
async def memory_manager() -> HTMLResponse:
    return HTMLResponse("""<!doctype html><title>Lumen memory</title><h1>Lumen memory</h1>
<form id=f><input id=q placeholder='Search'><button>Search</button></form><pre id=o></pre>
<script>f.onsubmit=async e=>{e.preventDefault();let r=await fetch('/memory/debug-search',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:q.value,user_id:'VST'})});o.textContent=JSON.stringify(await r.json(),null,2)}</script>""")


@app.get("/memory/items")
async def memory_items(user_id: str = "VST", item_type: str = "all", limit: int = 100) -> dict[str, object]:
    items: dict[str, object] = {}
    if item_type in {"all", "facts"}:
        items["facts"] = memory_store.list_facts(user_id, limit=max(1, min(limit, 500)))
    if item_type in {"all", "turns"}:
        items["turns"] = [turn.model_dump() for turn in memory_store.list_turns(user_id=user_id, limit=max(1, min(limit, 500)))]
    if item_type in {"all", "knowledge"}:
        items["knowledge"] = [hit.model_dump() for hit in memory_store.list_embeddings(user_id=user_id, limit=max(1, min(limit, 500)))]
    return items


@app.post("/memory/debug-search")
async def memory_debug_search(payload: ChatRequest) -> dict[str, object]:
    return await chat_service.debug_memory_search(user_text=payload.text, user_id=_user_id(payload.user_id))


@app.post("/memory/delete")
async def memory_delete(payload: dict[str, object]) -> dict[str, int]:
    item_type, item_id, user_id = str(payload.get("type", "")), int(payload.get("id", 0)), _user_id(payload.get("user_id"))
    if item_type == "fact":
        return {"deleted": int(memory_store.delete_fact(user_id, item_id))}
    if item_type == "turn":
        return {"deleted": int(memory_store.delete_turn(item_id))}
    if item_type == "knowledge":
        return {"deleted": memory_store.delete_embedding_ids(user_id=user_id, embedding_ids=[item_id])}
    raise HTTPException(status_code=400, detail="Unknown memory item type.")


@app.get("/api/tags")
async def ollama_tags() -> dict[str, object]:
    model = settings.ollama_dialogue_model.strip() or settings.ollama_model
    return {"models": [{"name": model, "model": model, "modified_at": datetime.now(timezone.utc).isoformat()}]}


@app.post("/api/show")
async def ollama_show(payload: OllamaCompatibleShowRequest) -> dict[str, object]:
    return {"modelfile": "FROM lumen", "parameters": "", "template": "", "details": {"family": "lumen"}}


@app.post("/api/chat")
async def ollama_chat(payload: OllamaCompatibleChatRequest):
    text = _message_text(payload.messages[-1].content) if payload.messages else ""
    if not text:
        raise HTTPException(status_code=400, detail="messages must contain user text")
    result = await chat_service.process_chat_request_result(
        ChatRequest(text=text, user_id=_user_id(payload.user_id)), _request_id(), deliver=False
    )
    response = {"model": _ollama_model(payload.model), "created_at": datetime.now(timezone.utc).isoformat(), "message": {"role": "assistant", "content": result.answer}, "done": True}
    return _ollama_stream(response) if payload.stream else response


@app.post("/api/generate")
async def ollama_generate(payload: OllamaCompatibleGenerateRequest):
    text = payload.prompt.strip()
    if not text:
        raise HTTPException(status_code=400, detail="prompt must not be empty")
    result = await chat_service.process_chat_request_result(
        ChatRequest(text=text, user_id=_user_id(payload.user_id)), _request_id(), deliver=False
    )
    response = {"model": _ollama_model(payload.model), "created_at": datetime.now(timezone.utc).isoformat(), "response": result.answer, "done": True}
    return _ollama_stream(response) if payload.stream else response


if __name__ == "__main__":
    uvicorn.run(app, host=settings.lumen_host, port=settings.lumen_port)

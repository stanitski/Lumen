"""HTTP entrypoint for Lumen's chat, memory, and knowledge runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
    NewsProcessingRequest,
    NewsProcessingResult,
    OllamaCompatibleChatRequest,
    OllamaCompatibleGenerateRequest,
    OllamaCompatibleShowRequest,
)
from lumen.services.chat_only import ChatOnlyService
from lumen.services.context_executor import ContextExecutor
from lumen.timezones import kyiv_timezone
from lumen.services.delivery import DeliveryService
from lumen.services.calendar_actions import CalendarActionService
from lumen.services.home_assistant_calendar import CalendarEvent, HomeAssistantCalendarClient
from lumen.services.knowledge import KnowledgeService
from lumen.services.memory import MemoryService
from lumen.services.news_processing import NewsProcessingBusyError, NewsProcessingService
from lumen.services.reminders import ReminderService, normalize_delivery_route
from lumen.services.reminder_wake import WindowsWakeAdapter
from lumen.services.speech_to_text import SpeechToTextError, SpeechToTextService
from lumen.services.telegram_context import TelegramContextService
from lumen.services.web_search import DuckDuckGoWebSearch, WebPageExtractor
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
reminder_service = ReminderService(
    database.session,
    delivery_service,
    WindowsWakeAdapter(db_path=database.db_path),
    logger=logger,
)


def _calendar_source_prefix(entity_id: str) -> str:
    return f"ha_calendar:{entity_id}:"


def _calendar_source_ref(event: CalendarEvent) -> str | None:
    if not event.uid:
        return None
    occurrence = event.recurrence_id or event.start.isoformat()
    digest = hashlib.sha256(f"{event.uid}\0{occurrence}".encode("utf-8")).hexdigest()
    return _calendar_source_prefix(event.calendar_entity_id) + digest


def _calendar_reminder_text(event: CalendarEvent) -> str:
    return "\n".join(part for part in (event.summary, event.description) if part).strip()


_calendar_delivery_route = normalize_delivery_route(
    {
        "target_channel": "telegram",
        "target_chat_id": settings.home_assistant_calendar_target_chat_id,
    }
)


async def _project_calendar_events(entity_id: str, events: tuple[CalendarEvent, ...]) -> None:
    if _calendar_delivery_route is None:
        return
    active_source_refs: set[str] = set()
    user_id = settings.lumen_user_id.strip() or "VST"
    for event in events:
        source_ref = _calendar_source_ref(event)
        text = _calendar_reminder_text(event)
        if source_ref is None:
            logger.warning("Calendar event skipped because uid is missing entity=%s start=%s", entity_id, event.start)
            continue
        if not text:
            logger.warning("Calendar event skipped because summary and description are empty entity=%s uid=%s", entity_id, event.uid)
            continue
        reminder_service.upsert_calendar_reminder(
            user_id=user_id,
            source_ref=source_ref,
            text=text,
            notification_at=event.notification_at,
            delivery_route=_calendar_delivery_route,
        )
        active_source_refs.add(source_ref)
    reminder_service.reconcile_calendar_reminders(
        user_id=user_id,
        source_prefix=_calendar_source_prefix(entity_id),
        active_source_refs=active_source_refs,
    )


calendar_client: HomeAssistantCalendarClient | None = None
if (
    settings.home_assistant_token.strip()
    and settings.home_assistant_calendar_entity_id_list
    and _calendar_delivery_route is not None
):
    calendar_client = HomeAssistantCalendarClient(
        base_url=settings.home_assistant_url,
        token=settings.home_assistant_token,
        calendar_entity_ids=settings.home_assistant_calendar_entity_id_list,
        on_snapshot=_project_calendar_events,
        on_update=_project_calendar_events,
        logger=logger,
        all_day_notification_time=settings.home_assistant_calendar_all_day_notification_time,
        reconnect_delay_seconds=settings.home_assistant_calendar_reconnect_seconds,
    )
elif settings.home_assistant_calendar_entity_id_list:
    logger.warning(
        "Home Assistant calendar projection disabled: token or canonical Telegram target chat id is missing"
    )


async def _refresh_calendar_after_create() -> None:
    if calendar_client is not None:
        await calendar_client.snapshot_today()


calendar_action_service: CalendarActionService | None = None
if settings.home_assistant_token.strip() and settings.home_assistant_calendar_write_entity_id.strip():
    calendar_action_service = CalendarActionService(
        base_url=settings.home_assistant_url,
        token=settings.home_assistant_token,
        write_entity_id=settings.home_assistant_calendar_write_entity_id,
        default_duration_minutes=settings.home_assistant_calendar_default_duration_minutes,
        refresh_callback=_refresh_calendar_after_create,
    )
elif settings.home_assistant_calendar_write_entity_id.strip():
    logger.warning("Home Assistant calendar creation disabled: token is missing")
memory_service = MemoryService(memory_store, ollama, settings, default_user_id="global", logger=logger)
speech_to_text_service = SpeechToTextService(settings, logger=logger)
telegram_context_service = TelegramContextService(settings, memory_store, speech_to_text_service, logger=logger)
web_page_extractor = WebPageExtractor(logger=logger)
web_search = DuckDuckGoWebSearch(logger=logger, page_extractor=web_page_extractor)
context_executor = ContextExecutor(telegram_context_service, web_search, logger=logger)
news_processing_service = NewsProcessingService(
    extractor=web_page_extractor,
    ollama=ollama,
    memory_service=memory_service,
    default_user_id=settings.lumen_user_id.strip() or "VST",
    logger=logger,
)
chat_service = ChatOnlyService(
    ollama=ollama,
    memory_service=memory_service,
    delivery_service=delivery_service,
    settings=settings,
    context_executor=context_executor,
    reminder_service=reminder_service,
    calendar_action_service=calendar_action_service,
    ollama_stream_idle_timeout_seconds=settings.ollama_stream_idle_timeout_seconds,
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


@app.on_event("startup")
async def start_reminder_recovery() -> None:
    # Handles missed due times after Lumen or Windows was unavailable.
    app.state.reminder_recovery_task = asyncio.create_task(reminder_service.run_due())
    if calendar_client is not None:
        app.state.home_assistant_calendar_task = asyncio.create_task(calendar_client.run_forever())


@app.on_event("shutdown")
async def stop_home_assistant_calendar() -> None:
    if calendar_client is None:
        return
    calendar_client.stop()
    task = getattr(app.state, "home_assistant_calendar_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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


def _ollama_stream_idle_error() -> JSONResponse:
    return JSONResponse(status_code=504, content={"error": "Ollama stream stalled"})


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
    if payload.event_type == "reminder_confirmed":
        answer = str(payload.data.get("answer") or payload.text or "Так").strip()
        reminder_id = payload.data.get("reminder_id")
        delivery_id = payload.data.get("delivery_id")
        if not answer or reminder_id is None or not str(delivery_id or "").strip():
            return ConnectorResponse(request_id=request_id, status="ignored", handled=False, reason="legacy_reminder_confirmation_missing_delivery_id")
        user_text = answer
    elif payload.event_type == "attachment" and payload.file is not None:
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


@app.post("/news/process", response_model=NewsProcessingResult)
async def process_news(payload: NewsProcessingRequest) -> NewsProcessingResult:
    try:
        return await news_processing_service.process(payload, user_id=_user_id(payload.user_id))
    except NewsProcessingBusyError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


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
    if result.execution_outcome == "cancelled_idle":
        return _ollama_stream_idle_error()
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
    if result.execution_outcome == "cancelled_idle":
        return _ollama_stream_idle_error()
    response = {"model": _ollama_model(payload.model), "created_at": datetime.now(timezone.utc).isoformat(), "response": result.answer, "done": True}
    return _ollama_stream(response) if payload.stream else response


if __name__ == "__main__":
    if "--timezone-smoke" in sys.argv:
        print(f"Timezone OK: {kyiv_timezone().key}")
    elif "--run-reminders" in sys.argv:
        asyncio.run(reminder_service.run_due())
    else:
        uvicorn.run(app, host=settings.lumen_host, port=settings.lumen_port)

"""HTTP entrypoint for Lumen's chat, memory, and knowledge runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen.config import Settings
from lumen.connectors.ollama import OllamaConnector
from lumen.memory.store import MemoryStore
from lumen.memory_admin_ui import MEMORY_ADMIN_HTML
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
from lumen.services.conversation import ConversationService
from lumen.services.calendar_actions import CalendarActionService
from lumen.services.home_assistant_calendar import CalendarEvent, HomeAssistantCalendarClient
from lumen.services.knowledge import KnowledgeService
from lumen.services.memory import MemoryService
from lumen.services.memory_admin import (
    FactDelete, FactEdit, MemoryAdminService, MemoryPreview, require_local_admin,
)
from lumen.services.news_processing import NewsProcessingBusyError, NewsProcessingService
from lumen.services.nutrition import NutritionService
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
memory_service = MemoryService(memory_store, ollama, settings, default_user_id="global", logger=logger)
conversation_service = ConversationService(memory_service, delivery_service, logger=logger,
    task_starter=lambda coroutine: _start_background_task(coroutine, label="conversation indexing"))
reminder_service = ReminderService(
    database.session,
    delivery_service,
    WindowsWakeAdapter(db_path=database.db_path),
    logger=logger,
    missed_grace_minutes=settings.reminder_grace_minutes,
    default_snooze_minutes=settings.reminder_snooze_minutes,
    management_session_minutes=settings.reminder_management_session_ttl_minutes,
    conversation_service=conversation_service,
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
    user_id = settings.lumen_user_id
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
speech_to_text_service = SpeechToTextService(settings, logger=logger)
nutrition_service = NutritionService(settings, ollama, delivery_service, logger=logger, conversation_service=conversation_service)
telegram_context_service = TelegramContextService(settings, memory_store, speech_to_text_service, logger=logger)
web_page_extractor = WebPageExtractor(logger=logger)
web_search = DuckDuckGoWebSearch(logger=logger, page_extractor=web_page_extractor)
context_executor = ContextExecutor(telegram_context_service, web_search, logger=logger,
                                   nutrition_store=nutrition_service.store if settings.nutrition_enabled else None, nutrition_timezone=nutrition_service.timezone)
news_processing_service = NewsProcessingService(
    extractor=web_page_extractor,
    ollama=ollama,
    memory_service=memory_service,
    default_user_id=settings.lumen_user_id,
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
    conversation_service=conversation_service,
    ollama_stream_idle_timeout_seconds=settings.ollama_stream_idle_timeout_seconds,
    logger=logger,
)
knowledge_service = KnowledgeService(
    memory_store,
    ollama,
    settings,
    default_user_id=settings.lumen_user_id,
    logger=logger,
)
memory_admin_service = MemoryAdminService(
    memory_store, fact_context=memory_service._fact_context, chat_service=chat_service,
    user_id=settings.lumen_user_id, logger=logger,
)
app = FastAPI(title="Lumen")
app.state.background_tasks = set()


def _start_background_task(coroutine: Coroutine[Any, Any, None], *, label: str) -> asyncio.Task[None]:
    task = asyncio.create_task(coroutine)
    app.state.background_tasks.add(task)

    def on_done(completed: asyncio.Task[None]) -> None:
        app.state.background_tasks.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            logger.info("Background task cancelled: %s", label)
        except Exception:
            logger.exception("Background task failed: %s", label)

    task.add_done_callback(on_done)
    return task


@app.on_event("startup")
async def start_reminder_recovery() -> None:
    _start_background_task(memory_service.recover_unprocessed_dialogue_turns(user_id=settings.lumen_user_id),
                           label="dialogue index recovery")
    # Handles missed due times after Lumen or Windows was unavailable.
    app.state.reminder_recovery_task = _start_background_task(reminder_service.run_due(), label="reminder recovery")
    if calendar_client is not None:
        app.state.home_assistant_calendar_task = _start_background_task(calendar_client.run_forever(), label="Home Assistant calendar")


@app.on_event("shutdown")
async def stop_home_assistant_calendar() -> None:
    if calendar_client is not None:
        calendar_client.stop()
    tasks = tuple(app.state.background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _user_id(value: object | None) -> str:
    return str(value or settings.lumen_user_id).strip() or settings.lumen_user_id


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
    _start_background_task(chat_service.process_chat_request(payload, request_id), label=f"chat request={request_id}")
    return ChatAcceptedResponse(request_id=request_id)


@app.post("/connector", response_model=ConnectorResponse, status_code=202)
async def connector(payload: ConnectorRequest) -> ConnectorResponse:
    request_id = _request_id()
    callback_metadata = _callback_metadata(payload)
    if payload.event_type == "nutrition_delivery":
        _start_background_task(_process_nutrition_event(payload, request_id, callback_metadata),
                               label=f"nutrition delivery request={request_id}")
        return ConnectorResponse(request_id=request_id, handled=True, reason="nutrition_delivery")
    if payload.event_type == "reminder_confirmed":
        answer = str(payload.data.get("answer") or payload.text or "Так").strip()
        reminder_id = payload.data.get("reminder_id")
        delivery_id = payload.data.get("delivery_id")
        if not answer or reminder_id is None or not str(delivery_id or "").strip():
            return ConnectorResponse(request_id=request_id, status="ignored", handled=False, reason="legacy_reminder_confirmation_missing_delivery_id")
        user_text = answer
    elif payload.event_type == "attachment" and payload.file is not None:
        if settings.nutrition_enabled and payload.source == "telegram" and nutrition_service.is_image(payload.file):
            _start_background_task(
                _process_nutrition_event(payload, request_id, callback_metadata),
                label=f"nutrition request={request_id}",
            )
            return ConnectorResponse(request_id=request_id, handled=True)
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
            request = ChatRequest(text=payload.text or "[Голосове/відеоповідомлення]", user_id=_user_id(payload.user_id), metadata=dict(payload.data))
            await conversation_service.finish(
                user_id=request.user_id, question=request.text,
                answer="Не вдалося розпізнати голосове або відеоповідомлення. Спробуй надіслати його ще раз.",
                request_id=request_id, source=payload.source, kind="speech", execution_outcome="error",
                event_id=chat_service._event_id(request, callback_metadata), callback_metadata=callback_metadata,
                store_memory=request.user_id.casefold() != "global" and not chat_service._memory_disabled(payload.data, callback_metadata))
            return ConnectorResponse(request_id=request_id, status="ignored", handled=False, reason="speech_transcription_failed")
        text = str(payload.text or "").strip()
        user_text = f"{text}\n\n{transcript}" if text else transcript
    elif payload.event_type == "text" and (payload.text or "").strip():
        user_text = str(payload.text)
    else:
        return ConnectorResponse(request_id=request_id, status="ignored", handled=False, reason="unsupported_connector_event")
    _start_background_task(
        _process_connector_text(payload, user_text, request_id, callback_metadata),
        label=f"connector request={request_id}",
    )
    return ConnectorResponse(request_id=request_id, handled=True)


async def _process_connector_text(payload: ConnectorRequest, text: str, request_id: str,
                                  callback_metadata: dict[str, object]) -> None:
    normalized = payload.model_copy(update={"text": text, "event_type": "text" if payload.event_type == "attachment" else payload.event_type, "file": None})
    await chat_service.process_connector_request(normalized, request_id,
                                                 callback_metadata=callback_metadata,
                                                 nutrition_service=nutrition_service)


async def _process_nutrition_event(payload: ConnectorRequest, request_id: str,
                                   callback_metadata: dict[str, object]) -> None:
    if payload.event_type == "nutrition_delivery":
        await nutrition_service.handle(payload, user_id=_user_id(payload.user_id), request_id=request_id,
                                       callback_metadata=callback_metadata)
    else:
        await chat_service.process_connector_request(payload, request_id,
                                                     callback_metadata=callback_metadata,
                                                     nutrition_service=nutrition_service)


@app.post("/knowledge/upload", response_model=KnowledgeUploadResult)
async def upload_knowledge(
    file: UploadFile = File(...),
    scope: KnowledgeScope = Form("personal"),
    user_id: str | None = Form(None),
    source_ref: str | None = Form(None),
    max_chunk_chars: int = Form(8000),
) -> KnowledgeUploadResult:
    request_id = _request_id()
    effective_user = _user_id(user_id)
    question = f"[Завантаження знань] {file.filename or 'document'}"
    try:
        result = await knowledge_service.upload(
            file=file, user_id=user_id, scope=scope, source_ref=source_ref, max_chunk_chars=max_chunk_chars)
    except Exception:
        await conversation_service.finish(user_id=effective_user, question=question,
            answer="Не вдалося завантажити документ до бази знань.", request_id=request_id,
            source="knowledge-api", kind="knowledge", execution_outcome="error", deliver=False)
        raise
    history = await conversation_service.finish(user_id=effective_user, question=question,
        answer=result.message, request_id=request_id, source="knowledge-api", kind="knowledge",
        turn_meta={"source_ref": result.source_ref, "scope": result.scope}, deliver=False)
    if history.persistence_outcome == "failed":
        # Ingestion has succeeded; don't misrepresent it as a failed upload or invite a duplicate.
        logger.error("Knowledge uploaded but conversation persistence failed request=%s", request_id)
    return result


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


@app.get("/memory", response_class=HTMLResponse, dependencies=[Depends(require_local_admin)])
async def memory_manager() -> HTMLResponse:
    return HTMLResponse(MEMORY_ADMIN_HTML, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })


@app.get("/memory/admin/items", dependencies=[Depends(require_local_admin)])
async def memory_admin_items() -> dict:
    return await asyncio.to_thread(memory_admin_service.items)


@app.get("/memory/admin/facts/{fact_id}", dependencies=[Depends(require_local_admin)])
async def memory_admin_fact(fact_id: int) -> dict:
    return await asyncio.to_thread(memory_admin_service.fact_details, fact_id)


@app.patch("/memory/admin/facts/{fact_id}", dependencies=[Depends(require_local_admin)])
async def memory_admin_update(fact_id: int, payload: FactEdit) -> dict:
    return await memory_admin_service.update_fact(fact_id, payload)


@app.delete("/memory/admin/facts/{fact_id}", dependencies=[Depends(require_local_admin)])
async def memory_admin_delete(fact_id: int, payload: FactDelete) -> dict:
    return await asyncio.to_thread(memory_admin_service.delete_fact, fact_id, payload.revision)


@app.post("/memory/admin/preview", dependencies=[Depends(require_local_admin)])
async def memory_admin_preview(payload: MemoryPreview) -> dict:
    try:
        return await memory_admin_service.preview(payload.text, payload.generate_answer)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Admin memory retrieval failed: %s", type(exc).__name__)
        raise HTTPException(502, "Не вдалося отримати контекст пам’яті. Спробуй ще раз.") from exc


@app.get("/memory/items")
async def memory_items(user_id: str | None = None, item_type: str = "all", limit: int = 100) -> dict[str, object]:
    user_id = _user_id(user_id)
    items: dict[str, object] = {}
    if item_type in {"all", "facts"}:
        items["facts"] = memory_store.list_facts(user_id, limit=max(1, min(limit, 500)))
    if item_type in {"all", "turns"}:
        items["turns"] = [turn.model_dump() for turn in memory_store.list_turns(user_id=user_id, limit=max(1, min(limit, 500)))]
    if item_type in {"all", "knowledge"}:
        items["knowledge"] = [hit.model_dump() for hit in memory_store.list_embeddings(
            user_id=user_id, limit=max(1, min(limit, 500)),
            source_types=["knowledge_text_personal", "knowledge_text_global"],
        )]
    return items


@app.post("/memory/debug-search")
async def memory_debug_search(payload: ChatRequest) -> dict[str, object]:
    return await chat_service.debug_memory_search(user_text=payload.text, user_id=_user_id(payload.user_id))


@app.post("/memory/delete")
async def memory_delete(payload: dict[str, object]) -> dict[str, int]:
    raw_id = payload.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or not str(raw_id).strip().isdecimal():
        raise HTTPException(status_code=400, detail="Item id must be a positive integer.")
    item_id = int(raw_id)
    if item_id <= 0:
        raise HTTPException(status_code=400, detail="Item id must be a positive integer.")
    item_type, user_id = str(payload.get("type", "")), _user_id(payload.get("user_id"))
    if item_type == "fact":
        return {"deleted": int(memory_store.delete_fact(user_id, item_id))}
    if item_type == "turn":
        return {"deleted": memory_store.delete_turn_ids(user_id=user_id, turn_ids=[item_id])}
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

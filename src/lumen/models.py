"""Pydantic models and shared enums for chat, memory, reminders, delivery, and knowledge APIs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MemoryCategory = Literal[
    "preference",
    "profile",
    "routine",
    "rule",
    "relationship",
    "project",
    "secret",
    "credential",
    "device_alias",
    "home_context",
    "news_context",
    "messenger_context",
]

RetentionPolicy = Literal["permanent", "ephemeral_24h", "ephemeral_7d", "ephemeral_14d", "ephemeral_30d"]

MemoryPredicate = Literal[
    "is",
    "prefers",
    "likes",
    "dislikes",
    "alias",
    "uses",
    "owns",
    "works_at",
    "lives_in",
    "role",
    "preferred_name",
]

KnowledgeUploadKind = Literal["text", "pdf", "docx", "xlsx", "pptx", "image", "video", "unsupported"]
KnowledgeUploadStatus = Literal["indexed", "pending", "unsupported"]
KnowledgeUploadSessionStatus = Literal["pending_scope", "completed", "failed", "cancelled"]
ExecutionOutcome = Literal["ok", "partial", "error", "awaiting_confirmation", "cancelled_idle"]
PersistenceOutcome = Literal["stored", "skipped", "failed"]
DeliveryOutcome = Literal["delivered", "domain_delivered", "inline", "suppressed", "failed", "not_attempted"]
KnowledgeScope = Literal["personal", "global"]
DeliveryKind = Literal["chat", "reminder", "knowledge"]
ConnectorEventType = Literal[
    "text",
    "command",
    "attachment",
    "button_press",
    "reminder_confirmed",
    "knowledge_scope_selected",
    "nutrition_delivery",
]
ConnectorStatus = Literal["accepted", "pending", "ignored"]
NewsProcessingStatus = Literal["keep", "skip", "retry"]
NewsContentSource = Literal["article", "rss"]


class ConversationTurnRecord(BaseModel):
    id: int
    user_id: str
    source: str
    question: str
    answer: str
    question_created_at: str
    answer_created_at: str
    created_at: str
    memory_processed: int
    turn_meta: dict[str, object] = Field(default_factory=dict)
    primary_topic: str = ""
    primary_topic_embedding: list[float] = Field(default_factory=list)
    question_embedding: list[float] = Field(default_factory=list)
    answer_embedding: list[float] = Field(default_factory=list)


class ChatRequest(BaseModel):
    text: str
    user_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class MemoryFact(BaseModel):
    user_id: str
    category: MemoryCategory
    subject: str
    predicate: MemoryPredicate
    value: str
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    importance: int = Field(default=6, ge=1, le=10)
    tags: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    retention_policy: RetentionPolicy = "permanent"


class EmbeddingHit(BaseModel):
    id: int
    user_id: str
    source_type: str
    source_ref: str
    chunk_index: int
    content: str
    importance: int
    similarity: float
    created_at: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)
    expires_at: str | None = None


class NewsProcessingRequest(BaseModel):
    """One raw item received from the Feedreader queue."""

    user_id: str | None = None
    feed_url: str = ""
    title: str = ""
    link: str = ""
    description: str = ""
    content: str = ""
    published_at: str | None = None
    received_at: str | None = None


class NewsTodoItem(BaseModel):
    item: str
    description: str


class NewsFiveW(BaseModel):
    what: str = ""
    who: str = ""
    where: str = ""
    when: str = ""
    why_or_context: str = ""
    narration: str
    missing_fields: list[str] = Field(default_factory=list)


class NewsProcessingResult(BaseModel):
    status: NewsProcessingStatus
    news_id: str | None = None
    content_source: NewsContentSource | None = None
    canonical_url: str = ""
    published_at: str | None = None
    todo_item: NewsTodoItem | None = None
    reason_code: str | None = None
    reason: str | None = None
    missing_5w: list[str] = Field(default_factory=list)


class ChatResult(BaseModel):
    request_id: str
    kind: Literal["chat"] = "chat"
    user_id: str
    answer: str
    voice: bool = False
    delivered: bool = False
    execution_outcome: ExecutionOutcome = "ok"
    persistence_outcome: PersistenceOutcome = "skipped"
    delivery_outcome: DeliveryOutcome = "not_attempted"
    metadata: dict[str, object] = Field(default_factory=dict)


class ChatAcceptedResponse(BaseModel):
    request_id: str
    kind: Literal["chat"] = "chat"
    status: Literal["accepted"] = "accepted"


class ConnectorReplyTarget(BaseModel):
    target: int | str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class ConnectorFile(BaseModel):
    id: str | None = None
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    url: str | None = None


class ConnectorRequest(BaseModel):
    source: str = "api"
    event_type: ConnectorEventType = "text"
    user_id: int | str | None = None
    reply: ConnectorReplyTarget | None = None
    text: str | None = None
    command: str | None = None
    args: str | None = None
    data: dict[str, object] = Field(default_factory=dict)
    file: ConnectorFile | None = None

    @field_validator("file", mode="before")
    @classmethod
    def _empty_file_to_none(cls, value: object) -> object:
        if value == "":
            return None
        if isinstance(value, dict) and not any(str(item or "").strip() for item in value.values()):
            return None
        return value


class ConnectorResponse(BaseModel):
    request_id: str
    kind: Literal["connector"] = "connector"
    status: ConnectorStatus = "accepted"
    handled: bool = False
    reason: str = "accepted"


class OllamaCompatibleMessage(BaseModel):
    role: str
    content: str | list[dict[str, object]] = ""


class OllamaCompatibleChatRequest(BaseModel):
    model: str = "lumen"
    messages: list[OllamaCompatibleMessage] = Field(default_factory=list)
    stream: bool = True
    user_id: int | str | None = None
    options: dict[str, object] = Field(default_factory=dict)
    keep_alive: str | int | None = None


class OllamaCompatibleGenerateRequest(BaseModel):
    model: str = "lumen"
    prompt: str = ""
    system: str | None = None
    template: str | None = None
    context: list[int] | None = None
    stream: bool = True
    user_id: int | str | None = None
    options: dict[str, object] = Field(default_factory=dict)
    keep_alive: str | int | None = None


class OllamaCompatibleShowRequest(BaseModel):
    model: str = "lumen"


class HomeAssistantCallbackPayload(BaseModel):
    request_id: str
    kind: DeliveryKind
    user_id: str
    answer: str
    voice: bool
    voice_chunks: list[str] = Field(default_factory=list)
    reminder_id: int | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class SleepResult(BaseModel):
    processed_turns: int
    deleted_turns: int
    deleted_expired_facts: int = 0
    deleted_expired_embeddings: int = 0


class KnowledgeUploadResult(BaseModel):
    status: KnowledgeUploadStatus
    kind: KnowledgeUploadKind
    scope: KnowledgeScope
    user_id: str
    filename: str
    content_type: str
    source_ref: str
    chunks_created: int = 0
    embeddings_created: int = 0
    message: str


class KnowledgeUploadSessionRecord(BaseModel):
    id: int
    user_id: str
    source_ref: str
    file_name: str
    file_mime_type: str | None = None
    file_size: int | None = None
    file_id: str | None = None
    downloaded_file_path: str | None = None
    downloaded_file_url: str | None = None
    status: KnowledgeUploadSessionStatus
    created_at: str
    resolved_at: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class KnowledgeUploadRequest(BaseModel):
    user_id: str | None = None
    source_ref: str | None = None
    scope: KnowledgeScope = "personal"
    max_chunk_chars: int = Field(default=8000, ge=500, le=50000)

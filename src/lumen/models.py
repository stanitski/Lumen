from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
]

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

KnowledgeUploadKind = Literal["text", "pdf", "image", "video", "unsupported"]
KnowledgeUploadStatus = Literal["indexed", "pending", "unsupported"]
ReminderStatus = Literal["pending", "sent", "cancelled", "failed"]
PlannerActionType = Literal["memory_fact", "reminder", "reminder_list", "web_search"]
KnowledgeScope = Literal["personal", "global"]


class MemoryHit(BaseModel):
    id: int
    user_id: str
    category: MemoryCategory
    subject: str
    predicate: MemoryPredicate
    value: str
    confidence: float
    importance: int
    source_ref: str


class ConversationTurnRecord(BaseModel):
    id: int
    conversation_id: str
    user_id: str
    source: str
    question: str
    answer: str
    question_created_at: str
    answer_created_at: str
    created_at: str
    memory_processed: int


class ChatRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    user_id: str | None = None


class MemoryFact(BaseModel):
    user_id: str
    category: MemoryCategory
    subject: str
    predicate: MemoryPredicate
    value: str
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    importance: int = Field(default=6, ge=1, le=10)
    tags: list[str] = Field(default_factory=list)


class EmbeddingHit(BaseModel):
    id: int
    user_id: str
    source_type: str
    source_ref: str
    chunk_index: int
    role: str | None = None
    content: str
    importance: int
    similarity: float
    metadata: dict[str, object] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    answer: str
    memory_facts: list[MemoryFact] = Field(default_factory=list)


class SleepResult(BaseModel):
    processed_turns: int
    deleted_turns: int


class TelegramSendRequest(BaseModel):
    chat_id: int | str
    message: str
    parse_mode: str | None = None


class TelegramSendResult(BaseModel):
    ok: bool
    status_code: int
    response: dict[str, object] = Field(default_factory=dict)
    message: str = ""


class TalkSendRequest(BaseModel):
    message: str


class TalkSendResult(BaseModel):
    ok: bool
    status_code: int
    response: dict[str, object] = Field(default_factory=dict)
    message: str = ""


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


class ReminderActionPayload(BaseModel):
    text: str
    due_at: str | None = None
    duration_seconds: int | None = Field(default=None, ge=1)
    repeat_interval_seconds: int | None = Field(default=None, ge=1)
    repeat_until: str | None = None


class PlannerAction(BaseModel):
    type: PlannerActionType
    payload: dict[str, object] = Field(default_factory=dict)


class PlannerResponse(BaseModel):
    draft_answer: str
    actions: list[PlannerAction] = Field(default_factory=list)


class ActionExecutionResult(BaseModel):
    type: PlannerActionType
    ok: bool
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ReminderRecord(BaseModel):
    id: int
    user_id: str
    conversation_id: str | None = None
    source_ref: str
    text: str
    due_at: str
    status: ReminderStatus
    created_at: str
    sent_at: str | None = None
    repeat_interval_seconds: int | None = None
    repeat_until: str | None = None


class ReminderRequest(BaseModel):
    user_id: str
    conversation_id: str | None = None
    source_ref: str
    text: str
    due_at: str
    repeat_interval_seconds: int | None = Field(default=None, ge=1)
    repeat_until: str | None = None


class KnowledgeUploadRequest(BaseModel):
    user_id: str | None = None
    source_ref: str | None = None
    scope: KnowledgeScope = "personal"
    max_chunk_chars: int = Field(default=8000, ge=500, le=50000)

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
ReminderStatus = Literal["pending", "sent", "awaiting_confirmation", "cancelled", "failed"]
SkillScope = Literal["system", "user"]
IntentKind = Literal["conversational", "action"]
TaskStatus = Literal["pending", "active", "awaiting_input", "completed", "cancelled", "failed"]
TaskRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "timed_out"]
TaskExecutionMode = Literal["immediate", "scheduled", "recurring"]
TaskRepeatPolicy = Literal["fixed_interval", "after_completion"]
PlanDecision = Literal["ready_to_execute", "needs_clarification", "safe_response"]
PolicyOutcome = Literal["allow", "confirm", "deny"]
ExecutionOutcome = Literal["ok", "partial", "error", "awaiting_confirmation"]
DeliveryMode = Literal["callback", "inline", "none"]
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
]
ConnectorStatus = Literal["accepted", "pending", "ignored"]


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


class HomeAssistantEntityRecord(BaseModel):
    entity_id: str
    domain: str
    state: str
    friendly_name: str = ""
    unit_of_measurement: str = ""
    device_class: str = ""
    area_name: str = ""
    device_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, object] = Field(default_factory=dict)
    search_text: str = ""
    is_readable: bool = True
    is_actionable: bool = False
    last_synced_at: str


class SkillRecord(BaseModel):
    skill_id: str
    intent_id: str
    scope: SkillScope
    owner_user_id: str | None = None
    enabled: bool = True
    priority: int = 50
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    variables_schema: dict[str, object] = Field(default_factory=dict)
    steps: list[dict[str, object]] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list)
    content_hash: str = ""
    created_at: str
    updated_at: str

class IntentRecord(BaseModel):
    intent_id: str
    kind: IntentKind
    enabled: bool = True
    description: str = ""
    is_default: bool = False
    created_at: str
    updated_at: str


class SkillPlanItem(BaseModel):
    skill_id: str
    variables: dict[str, object] = Field(default_factory=dict)


class SkillPlanResponse(BaseModel):
    selected_skills: list[SkillPlanItem] = Field(default_factory=list)
    action_intent: str = ""
    decision: PlanDecision = "ready_to_execute"
    clarification_question: str = ""
    clarification_reason: str = ""
    debug_trace: dict[str, object] = Field(default_factory=dict)


class ExecutionAction(BaseModel):
    action_id: str
    skill_id: str
    variables: dict[str, object] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    expected_side_effects: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    plan_id: str
    request_id: str
    action_intent: str = ""
    actions: list[ExecutionAction] = Field(default_factory=list)
    clarification_required: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    action_id: str
    outcome: PolicyOutcome
    reason: str = ""
    confirmation_evidence: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class ActionExecutionResult(BaseModel):
    action_id: str
    skill_id: str
    outcome: ExecutionOutcome
    message: str = ""
    responses: list[str] = Field(default_factory=list)
    state_changes: list[dict[str, object]] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    plan_id: str
    request_id: str
    outcome: ExecutionOutcome
    actions: list[ActionExecutionResult] = Field(default_factory=list)
    primary_answer: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)


class DeliveryEnvelope(BaseModel):
    request_id: str
    user_id: str
    kind: DeliveryKind = "chat"
    mode: DeliveryMode = "callback"
    answer: str = ""
    voice: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    id: int
    user_id: str
    source_ref: str
    skill_id: str | None = None
    task_group_id: str | None = None
    parent_task_id: int | None = None
    status: TaskStatus = "pending"
    execution_mode: TaskExecutionMode = "immediate"
    due_at: str | None = None
    repeat_policy: TaskRepeatPolicy | None = None
    repeat_interval_seconds: int | None = None
    repeat_count: int | None = None
    repeat_until: str | None = None
    run_count: int = 0
    priority: int = 50
    payload: dict[str, object] = Field(default_factory=dict)
    steps: list[dict[str, object]] = Field(default_factory=list)
    requires_confirmation: bool = False
    created_at: str
    updated_at: str
    completed_at: str | None = None
    cancelled_at: str | None = None
    last_error: str = ""


class TaskRunRecord(BaseModel):
    id: int
    task_id: int
    task_group_id: str | None = None
    status: TaskRunStatus = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, object] = Field(default_factory=dict)
    error_text: str = ""
    cancelled_by_task_id: int | None = None


class ReminderActionPayload(BaseModel):
    text: str
    due_at: str | None = None
    duration_seconds: int | None = Field(default=None, ge=1)
    repeat_interval_seconds: int | None = Field(default=None, ge=1)
    repeat_until: str | None = None


class ReminderRecord(BaseModel):
    id: int
    user_id: str
    source_ref: str
    text: str
    due_at: str
    status: ReminderStatus
    created_at: str
    sent_at: str | None = None
    repeat_interval_seconds: int | None = None
    repeat_until: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class ReminderRequest(BaseModel):
    user_id: str
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

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentRequest(BaseModel):
    text: str
    source: str = "home_assistant"
    user_id: str = "default-user"
    session_id: str = "default-session"
    conversation_id: str = "default-conversation"
    home_id: str = "default-home"
    allow_actions: bool = True
    context_overrides: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    source_type: str
    source_ref: str
    title: str
    snippet: str


class MemoryHit(BaseModel):
    id: int
    category: str
    subject: str
    predicate: str
    value: str
    confidence: float
    importance: int
    source_ref: str


class KnowledgeHit(BaseModel):
    id: int
    document_id: int
    source_type: str
    source_ref: str
    title: str
    snippet: str
    score: int


class ActionProposal(BaseModel):
    action_id: str
    label: str
    ha_domain: str
    ha_service: str
    service_data: dict[str, Any] = Field(default_factory=dict)
    reason: str
    risk_level: str = "medium"


class AgentResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    memory_hits: list[MemoryHit] = Field(default_factory=list)
    knowledge_hits: list[KnowledgeHit] = Field(default_factory=list)
    action_proposal: ActionProposal | None = None
    requires_confirmation: bool = False


class ConfirmActionRequest(BaseModel):
    action_id: str
    confirmed: bool
    user_id: str = "default-user"
    conversation_id: str = "default-conversation"


class SearchRequest(BaseModel):
    query: str
    limit: int = 10


class ReindexRequest(BaseModel):
    paths: list[str] | None = None


class KnowledgeUploadRequest(BaseModel):
    filename: str
    content: str
    relative_path: str | None = None
    reindex_after_upload: bool = True


class MemoryFactPayload(BaseModel):
    category: str
    subject: str
    predicate: str
    value: str
    confidence: float = 0.8
    importance: int = 5
    source_ref: str = "admin:manual"
    tags: list[str] = Field(default_factory=list)
    expires_at: str | None = None


class MemoryFactTextPayload(BaseModel):
    text: str
    source_ref: str = "admin:manual"


class HomeAssistantAssistRequest(BaseModel):
    text: str
    user_id: str = "ha-user"
    conversation_id: str = "ha-conversation"
    session_id: str = "ha-session"
    language: str = "uk"
    allow_actions: bool = True
    exposed_entities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HomeAssistantAssistResponse(BaseModel):
    response_type: str = "query_answer"
    speech: str
    conversation_id: str
    continue_conversation: bool = True
    requires_confirmation: bool = False
    action_id: str | None = None
    action_label: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ConversationLogRecord:
    id: int
    conversation_id: str
    session_id: str
    user_id: str
    source: str
    role: str
    message: str
    created_at: str

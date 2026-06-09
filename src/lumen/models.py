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

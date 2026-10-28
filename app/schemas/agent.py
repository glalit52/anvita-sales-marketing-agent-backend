import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, HttpUrl

from app.schemas.common import Schema
from app.schemas.inbox import AgentMode

ToolName = Literal[
    "search_knowledge",
    "get_contact",
    "update_contact",
    "check_availability",
    "book_appointment",
    "cancel_appointment",
    "create_payment_link",
    "create_task",
    "handoff_to_human",
]


class Guardrails(Schema):
    max_discount_pct: int = Field(default=0, ge=0, le=100)
    max_payment_minor: int | None = Field(default=None, ge=0)
    blocked_topics: list[str] = Field(default_factory=list)
    require_grounding: bool = True


class AgentConfigOut(Schema):
    name: str
    tone: str
    instructions: str
    languages: list[str]
    default_mode: AgentMode
    guardrails: Guardrails
    version: int
    updated_at: datetime


class AgentConfigUpdate(Schema):
    name: str | None = Field(default=None, max_length=80)
    tone: str | None = Field(default=None, max_length=200)
    instructions: str | None = Field(default=None, max_length=20_000)
    languages: list[str] | None = None
    default_mode: AgentMode | None = None
    guardrails: Guardrails | None = None


class PublishIn(Schema):
    note: str | None = Field(default=None, max_length=300)


class AgentVersionOut(Schema):
    id: uuid.UUID
    version: int
    note: str | None
    created_by_id: uuid.UUID | None
    created_at: datetime


class KnowledgeSourceIn(Schema):
    kind: Literal["website", "faq", "text"]
    title: str = Field(max_length=300)
    url: HttpUrl | None = None
    content: str | None = Field(default=None, max_length=500_000)


class FaqIn(Schema):
    question: str = Field(max_length=500)
    answer: str = Field(max_length=5000)


class KnowledgeSourceOut(Schema):
    id: uuid.UUID
    kind: str
    title: str
    url: str | None
    status: str
    error: str | None
    chunk_count: int
    last_synced_at: datetime | None
    created_at: datetime


class KnowledgeHit(Schema):
    source_id: uuid.UUID
    source_title: str
    title: str
    content: str
    score: float


class PlaybookIn(Schema):
    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=500)
    triggers: list[str] = Field(default_factory=list)
    instructions: str = Field(max_length=20_000)
    tools: list[ToolName] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = 100


class PlaybookOut(PlaybookIn):
    id: uuid.UUID
    updated_at: datetime


class EscalationRuleIn(Schema):
    name: str = Field(max_length=200)
    kind: Literal["keyword", "sentiment", "intent", "vip"]
    condition: dict[str, Any] = Field(default_factory=dict)
    action: Literal["assign", "notify", "pause"] = "assign"
    assign_to_id: uuid.UUID | None = None
    enabled: bool = True


class EscalationRuleOut(EscalationRuleIn):
    id: uuid.UUID


class SandboxMessage(Schema):
    role: Literal["contact", "ai"]
    body: str


class SandboxIn(Schema):
    messages: list[SandboxMessage] = Field(min_length=1, max_length=40)
    channel: str = "webchat"


class SandboxOut(Schema):
    reply: str | None
    escalated: bool
    escalation_reason: str | None
    sources: list[str]
    tool_calls: list[dict[str, Any]]


class AgentRunOut(Schema):
    id: uuid.UUID
    conversation_id: uuid.UUID | None
    kind: str
    model: str
    mode: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: Decimal
    tool_calls: list[dict[str, Any]]
    outcome: str
    reason: str | None
    latency_ms: int
    created_at: datetime


class AgentStats(Schema):
    conversations: int
    ai_resolved: int
    escalated: int
    resolution_rate: float
    bookings_by_ai: int
    avg_latency_ms: int
    cost_usd: Decimal

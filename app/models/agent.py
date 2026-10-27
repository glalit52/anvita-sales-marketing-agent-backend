import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Computed, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin, utcnow


class AgentConfig(IdMixin, TenantMixin, TimestampMixin, Base):
    """The org's AI employee: persona, guardrails and per-channel mode. One per org."""

    __tablename__ = "agent_configs"
    __table_args__ = (UniqueConstraint("org_id"),)

    name: Mapped[str] = mapped_column(String(80), default="Anvita")
    tone: Mapped[str] = mapped_column(String(200), default="Warm, concise and professional")
    instructions: Mapped[str] = mapped_column(Text, default="")
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=lambda: ["English"])
    default_mode: Mapped[str] = mapped_column(String(16), default="autopilot")
    # {"max_discount_pct": 10, "max_payment_minor": 5000000, "blocked_topics": ["medical diagnosis"]}
    guardrails: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(default=1)


class AgentVersion(IdMixin, TenantMixin, Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("org_id", "version"),)

    version: Mapped[int]
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)


class KnowledgeSource(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_sources"

    kind: Mapped[str] = mapped_column(String(16))  # website | pdf | faq | text | sheet | gbp
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(2000))
    content: Mapped[str | None] = mapped_column(Text)  # raw text for faq / text sources
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | syncing | synced | error
    error: Mapped[str | None] = mapped_column(String(500))
    chunk_count: Mapped[int] = mapped_column(default=0)
    last_synced_at: Mapped[datetime | None]


class KnowledgeChunk(IdMixin, TenantMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_tsv", "tsv", postgresql_using="gin"),)

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int]
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    # 'simple' config: no stemming, so it behaves the same for Hinglish and regional-language text.
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', coalesce(title, '') || ' ' || content)", persisted=True)
    )


class Playbook(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "playbooks"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(500))
    # Intents / keywords that make this playbook relevant ("book", "price", "emi").
    triggers: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    instructions: Mapped[str] = mapped_column(Text)
    tools: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    channels: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)  # empty = all
    enabled: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[int] = mapped_column(default=100)


class EscalationRule(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "escalation_rules"

    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))  # keyword | sentiment | intent | vip
    # keyword: {"keywords": [...]}; sentiment: {"sentiment": "negative"}; intent: {"intents": [...]};
    # vip: {"tags": ["vip"]}
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    action: Mapped[str] = mapped_column(String(16), default="assign")  # assign | notify | pause
    assign_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(default=True)


class AgentRun(IdMixin, TenantMixin, Base):
    """One pass of the agent over a conversation: what it saw, which tools it called, what it cost."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_org_created", "org_id", "created_at"),)

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    trigger_message_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(String(16), default="reply")  # reply | classify | sandbox
    model: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    outcome: Mapped[str] = mapped_column(String(16))  # replied | drafted | escalated | skipped | error
    reason: Mapped[str | None] = mapped_column(String(300))
    latency_ms: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)

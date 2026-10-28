import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin, utcnow

CHANNEL_TYPES = ("whatsapp", "sms", "email", "webchat", "instagram", "facebook", "google", "voice", "linkedin")
AGENT_MODES = ("off", "copilot", "autopilot")


class Channel(IdMixin, TenantMixin, TimestampMixin, Base):
    """A connected inbox: one WhatsApp number, one mailbox, the website widget, etc."""

    __tablename__ = "channels"
    __table_args__ = (Index("ix_channels_type_external", "type", "external_id"),)

    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="connected")  # connected | disconnected | error
    # Provider-side id used to route inbound webhooks to this tenant (WhatsApp phone_number_id, mailbox …).
    external_id: Mapped[str | None] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Provider credentials (access tokens). Encrypt with a per-tenant KMS data key before going multi-region.
    credentials: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    agent_mode: Mapped[str] = mapped_column(String(16), default="autopilot")


class Conversation(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_org_status_last", "org_id", "status", "last_message_at"),
        Index("ix_conversations_contact_channel", "contact_id", "channel_type"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    channel_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | pending | closed
    assignee_type: Mapped[str] = mapped_column(String(8), default="ai")  # ai | human
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    ai_paused_until: Mapped[datetime | None]
    escalation_reason: Mapped[str | None] = mapped_column(String(300))
    unread_count: Mapped[int] = mapped_column(default=0)
    intent: Mapped[str | None] = mapped_column(String(120))
    sentiment: Mapped[str | None] = mapped_column(String(16))
    ai_summary: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None]
    last_message_preview: Mapped[str | None] = mapped_column(String(300))
    last_inbound_at: Mapped[datetime | None]


class Message(IdMixin, TenantMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index(
            "uq_messages_org_provider_msg",
            "org_id",
            "provider_msg_id",
            unique=True,
            postgresql_where=text("provider_msg_id IS NOT NULL"),
        ),
        Index(
            "uq_messages_org_idempotency",
            "org_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(String(8))  # inbound | outbound | internal
    author_type: Mapped[str] = mapped_column(String(8))  # contact | ai | human | system
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    author_name: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    is_note: Mapped[bool] = mapped_column(default=False)
    # Copilot mode: the AI drafts, a human approves and sends.
    is_draft: Mapped[bool] = mapped_column(default=False)
    card: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sources: Mapped[list[str]] = mapped_column(ARRAY(String(200)), default=list)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received|queued|sent|delivered|read|failed
    error: Mapped[str | None] = mapped_column(String(500))
    provider_msg_id: Mapped[str | None] = mapped_column(String(200))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    ai_run_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)

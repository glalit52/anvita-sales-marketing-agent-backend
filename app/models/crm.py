import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

STAGES = ("prospect", "new", "qualified", "booked", "customer", "lost")
DEAL_STAGES = ("new", "qualified", "booked", "proposal", "won", "lost")
DEAL_PROBABILITY = {"new": 10, "qualified": 30, "booked": 50, "proposal": 70, "won": 100, "lost": 0}


class Contact(IdMixin, TenantMixin, TimestampMixin, Base):
    """One customer graph: prospects, leads and customers are the same record moving through stages."""

    __tablename__ = "contacts"
    __table_args__ = (
        Index("uq_contacts_org_phone", "org_id", "phone", unique=True, postgresql_where=text("phone IS NOT NULL")),
        Index(
            "uq_contacts_org_email",
            "org_id",
            func.lower(text("email")),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index("ix_contacts_org_last_activity", "org_id", "last_activity_at"),
        Index("ix_contacts_tags", "tags", postgresql_using="gin"),
    )

    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))  # E.164
    email: Mapped[str | None] = mapped_column(String(320))
    company: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    stage: Mapped[str] = mapped_column(String(16), default="new")
    source: Mapped[str | None] = mapped_column(String(80))
    channel: Mapped[str | None] = mapped_column(String(16))  # preferred channel
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    # None means the AI agent owns the relationship.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    score: Mapped[int] = mapped_column(default=0)
    value_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    language: Mapped[str | None] = mapped_column(String(40))
    whatsapp_id: Mapped[str | None] = mapped_column(String(32))
    utm: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_activity_at: Mapped[datetime | None]


class ConsentRecord(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("contact_id", "channel", "purpose"),)

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16))  # whatsapp | sms | email
    purpose: Mapped[str] = mapped_column(String(16), default="marketing")  # marketing | transactional
    status: Mapped[str] = mapped_column(String(16))  # opted_in | opted_out | unknown
    source: Mapped[str] = mapped_column(String(120))
    captured_at: Mapped[datetime]


class ContactNote(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "contact_notes"

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(default=False)


class Deal(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "deals"
    __table_args__ = (Index("ix_deals_org_pipeline_stage", "org_id", "pipeline", "stage"),)

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    pipeline: Mapped[str] = mapped_column(String(32), default="default")
    stage: Mapped[str] = mapped_column(String(16), default="new")
    value_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    probability: Mapped[int] = mapped_column(default=10)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expected_close: Mapped[date | None]
    lost_reason: Mapped[str | None] = mapped_column(String(200))
    closed_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)


class Task(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_org_open_due", "org_id", "due_at", postgresql_where=text("done_at IS NULL")),)

    title: Mapped[str] = mapped_column(String(300))
    type: Mapped[str] = mapped_column(String(16), default="call")  # call | whatsapp | email | review | payment
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_type: Mapped[str] = mapped_column(String(8), default="user")  # user | ai | system
    created_by_id: Mapped[uuid.UUID | None]
    due_at: Mapped[datetime]
    has_time: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")  # high | normal | low
    done_at: Mapped[datetime | None]
    ai_context: Mapped[str | None] = mapped_column(Text)
    ai_confidence: Mapped[int | None]
    notes: Mapped[str | None] = mapped_column(Text)

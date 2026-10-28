import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, utcnow


class Notification(IdMixin, TenantMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_org_user_created", "org_id", "user_id", "created_at"),)

    # None = visible to the whole team.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    tab: Mapped[str] = mapped_column(String(16))  # mentions | escalations | ai | system
    icon: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    link: Mapped[str | None] = mapped_column(String(500))
    priority: Mapped[bool] = mapped_column(default=False)
    read_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)


class Event(IdMixin, Base):
    """Transactional outbox. Domain changes write an event in the same transaction; the worker
    delivers it to handlers (agent runtime, outbound sends, ingestion) at least once."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_pending", "available_at", postgresql_where=text("processed_at IS NULL")),)

    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    available_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)
    locked_until: Mapped[datetime | None]
    processed_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)

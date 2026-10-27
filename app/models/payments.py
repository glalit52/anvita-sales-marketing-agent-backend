import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

PAYMENT_STATUSES = ("pending", "paid", "overdue", "refunded", "cancelled", "expired")


class Payment(IdMixin, TenantMixin, TimestampMixin, Base):
    """A request for money (pay link / invoice) and, once paid, its settlement record."""

    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_org_status_created", "org_id", "status", "created_at"),)

    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id", ondelete="SET NULL"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    number: Mapped[str] = mapped_column(String(32))  # human-facing, e.g. INV-2026-0042
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    purpose: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # [{"description": str, "quantity": int, "unitMinor": int, "taxPct": float}]
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    method: Mapped[str | None] = mapped_column(String(16))  # upi | card | netbanking
    provider: Mapped[str] = mapped_column(String(16), default="test")
    provider_order_id: Mapped[str | None] = mapped_column(String(200), index=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(200))
    # Unguessable id used in the public /pay/{token} page.
    public_token: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_type: Mapped[str] = mapped_column(String(8), default="user")
    created_by_id: Mapped[uuid.UUID | None]
    due_at: Mapped[datetime | None]
    paid_at: Mapped[datetime | None]
    refunded_at: Mapped[datetime | None]

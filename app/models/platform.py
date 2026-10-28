import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin, utcnow


class IntegrationConnection(IdMixin, TenantMixin, TimestampMixin, Base):
    """A workspace's connection to a third-party app from the integrations catalog."""

    __tablename__ = "integration_connections"
    __table_args__ = (UniqueConstraint("org_id", "integration_id"),)

    integration_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="connected")  # connected | error | disconnected
    account: Mapped[str | None] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # API keys / OAuth tokens for this workspace. Encrypt with a per-tenant KMS key before multi-region.
    credentials: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(500))
    connected_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ApiKey(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(24))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class WebhookEndpoint(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "webhook_endpoints"

    url: Mapped[str] = mapped_column(String(2000))
    events: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    secret: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(default=True)
    success_count: Mapped[int] = mapped_column(default=0)
    failure_count: Mapped[int] = mapped_column(default=0)
    last_delivery_at: Mapped[datetime | None]
    last_status: Mapped[int | None]
    last_error: Mapped[str | None] = mapped_column(String(500))


class CreditLedger(IdMixin, TenantMixin, Base):
    """Usage-based credits (prospecting, AI conversations, voice). Positive = purchase/grant, negative = use."""

    __tablename__ = "credit_ledger"
    __table_args__ = (Index("ix_credit_ledger_org_meter_created", "org_id", "meter", "created_at"),)

    meter: Mapped[str] = mapped_column(String(24))  # prospect | ai | voice | wa
    delta: Mapped[int]
    reason: Mapped[str] = mapped_column(String(200))
    ref_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)


class BillingInvoice(IdMixin, TenantMixin, Base):
    """Anvita's own invoices to the workspace (subscription and credit packs)."""

    __tablename__ = "billing_invoices"

    number: Mapped[str] = mapped_column(String(32))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(16), default="paid")  # paid | open | refunded
    items: Mapped[str] = mapped_column(String(300))
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)


class DataRequest(IdMixin, TenantMixin, TimestampMixin, Base):
    """DPDP / GDPR data-subject request: export or erase everything held about a person."""

    __tablename__ = "data_requests"

    number: Mapped[str] = mapped_column(String(16))
    type: Mapped[str] = mapped_column(String(8))  # export | delete
    subject: Mapped[str] = mapped_column(String(200))  # phone, email or name as given
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(24), default="received")  # received | completed | rejected
    due_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)

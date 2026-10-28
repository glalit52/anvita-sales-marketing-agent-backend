import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

APPOINTMENT_STATUSES = ("unconfirmed", "confirmed", "checked-in", "completed", "no-show", "cancelled")


class Service(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "services"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    duration_min: Mapped[int] = mapped_column(default=30)
    price_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    deposit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    online: Mapped[bool] = mapped_column(default=True)  # bookable on the public page / by the AI
    active: Mapped[bool] = mapped_column(default=True)


class Provider(IdMixin, TenantMixin, TimestampMixin, Base):
    """Someone (or something, e.g. a chair) that can be booked."""

    __tablename__ = "providers"

    name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(120))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    location_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    service_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    active: Mapped[bool] = mapped_column(default=True)


class Appointment(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        # The database, not application code, guarantees a provider is never double-booked.
        ExcludeConstraint(
            ("provider_id", "="),
            (text("tstzrange(starts_at, ends_at)"), "&&"),
            name="ex_appointments_no_overlap",
            using="gist",
            where=text("status NOT IN ('cancelled', 'no-show')"),
        ),
        Index("ix_appointments_org_starts", "org_id", "starts_at"),
        Index(
            "uq_appointments_org_idempotency",
            "org_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    source: Mapped[str] = mapped_column(String(16), default="staff")  # staff | ai | online
    notes: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    cancelled_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(String(300))

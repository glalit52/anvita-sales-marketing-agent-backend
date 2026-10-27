import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin, utcnow

ROLES = ("owner", "admin", "manager", "agent")
ROLE_RANK = {r: i for i, r in enumerate(reversed(ROLES))}  # agent=0 … owner=3


class Organization(IdMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    country: Mapped[str] = mapped_column(String(2), default="IN")
    industry: Mapped[str | None] = mapped_column(String(80))
    plan: Mapped[str] = mapped_column(String(32), default="trial")
    # Facts the AI may quote: description, website, phone, address, policies, hours summary.
    business_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    last_login_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]


class Membership(IdMixin, TimestampMixin, Base):
    """A user's seat in an organisation. Not RLS-protected because login must list a user's orgs."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    # Empty list = access to every location.
    location_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)


class RefreshToken(IdMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    user_agent: Mapped[str | None] = mapped_column(String(300))


class Invite(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "invites"

    email: Mapped[str] = mapped_column(CITEXT)
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]


class Location(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "locations"

    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # [{"day": 0-6 (Mon=0), "open": bool, "from": "09:00", "to": "20:00"}]
    working_hours: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(default=True)


class AuditLog(IdMixin, TenantMixin, Base):
    """Append-only record of who (user, AI or system) did what, on what evidence."""

    __tablename__ = "audit_logs"

    actor_type: Mapped[str] = mapped_column(String(16))  # user | ai | system | api
    actor_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID | None]
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow, index=True)

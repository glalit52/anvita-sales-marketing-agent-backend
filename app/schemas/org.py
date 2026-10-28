import uuid
from datetime import datetime
from typing import Any

from pydantic import EmailStr, Field

from app.schemas.auth import Role
from app.schemas.common import Schema


class OrgOut(Schema):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    currency: str
    country: str
    industry: str | None
    plan: str
    business_profile: dict[str, Any]
    created_at: datetime


class OrgUpdate(Schema):
    name: str | None = Field(default=None, max_length=200)
    timezone: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    industry: str | None = None
    business_profile: dict[str, Any] | None = None


class WorkingHours(Schema):
    day: int = Field(ge=0, le=6)  # Mon=0
    open: bool = True
    from_: str = Field(default="09:00", alias="from", pattern=r"^\d{2}:\d{2}$")
    to: str = Field(default="18:00", pattern=r"^\d{2}:\d{2}$")


class LocationIn(Schema):
    name: str = Field(max_length=200)
    address: str | None = None
    phone: str | None = None
    timezone: str | None = None
    working_hours: list[WorkingHours] = Field(default_factory=list)
    active: bool = True


class LocationOut(LocationIn):
    id: uuid.UUID


class MemberOut(Schema):
    id: uuid.UUID  # user id
    name: str
    email: str
    role: Role
    online: bool
    location_ids: list[uuid.UUID]
    last_seen_at: datetime | None


class MemberUpdate(Schema):
    role: Role | None = None
    location_ids: list[uuid.UUID] | None = None


class InviteIn(Schema):
    email: EmailStr
    role: Role = "agent"


class InviteOut(Schema):
    id: uuid.UUID
    email: str
    role: Role
    expires_at: datetime
    created_at: datetime
    accepted_at: datetime | None
    # Only returned once, on creation, so the inviter can share the link.
    invite_url: str | None = None


class AuditOut(Schema):
    id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    data: dict[str, Any]
    created_at: datetime

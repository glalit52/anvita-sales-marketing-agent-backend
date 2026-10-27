import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import Schema

AppointmentStatus = Literal["unconfirmed", "confirmed", "checked-in", "completed", "no-show", "cancelled"]


class ServiceIn(Schema):
    name: str = Field(max_length=200)
    description: str | None = None
    duration_min: int = Field(default=30, ge=5, le=8 * 60)
    price_minor: int = Field(default=0, ge=0)
    deposit_minor: int = Field(default=0, ge=0)
    online: bool = True
    active: bool = True


class ServiceOut(ServiceIn):
    id: uuid.UUID


class ProviderIn(Schema):
    name: str = Field(max_length=200)
    title: str | None = None
    user_id: uuid.UUID | None = None
    location_ids: list[uuid.UUID] = Field(default_factory=list)
    service_ids: list[uuid.UUID] = Field(default_factory=list)
    active: bool = True


class ProviderOut(ProviderIn):
    id: uuid.UUID


class Slot(Schema):
    starts_at: datetime
    ends_at: datetime
    provider_id: uuid.UUID
    location_id: uuid.UUID


class AvailabilityQuery(Schema):
    service_id: uuid.UUID
    location_id: uuid.UUID | None = None
    provider_id: uuid.UUID | None = None
    start: date
    days: int = Field(default=7, ge=1, le=31)


class AppointmentIn(Schema):
    contact_id: uuid.UUID
    service_id: uuid.UUID
    provider_id: uuid.UUID | None = None  # None = first available provider
    location_id: uuid.UUID
    starts_at: datetime
    status: AppointmentStatus = "confirmed"
    notes: str | None = None
    deal_id: uuid.UUID | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class AppointmentUpdate(Schema):
    starts_at: datetime | None = None
    provider_id: uuid.UUID | None = None
    status: AppointmentStatus | None = None
    notes: str | None = None
    cancel_reason: str | None = None


class AppointmentOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    service_id: uuid.UUID
    provider_id: uuid.UUID
    location_id: uuid.UUID
    deal_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    source: str
    notes: str | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    created_at: datetime


class CalendarRange(Schema):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check(self) -> "CalendarRange":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

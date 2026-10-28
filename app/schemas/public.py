import uuid
from datetime import datetime
from typing import Any

from pydantic import EmailStr, Field, model_validator

from app.schemas.common import Schema
from app.schemas.org import WorkingHours
from app.schemas.payments import InvoiceLine


class PublicLocation(Schema):
    id: uuid.UUID
    name: str
    address: str | None
    phone: str | None
    working_hours: list[WorkingHours]


class PublicService(Schema):
    id: uuid.UUID
    name: str
    description: str | None
    duration_min: int
    price_minor: int
    deposit_minor: int


class PublicProvider(Schema):
    id: uuid.UUID
    name: str
    title: str | None
    location_ids: list[uuid.UUID]
    service_ids: list[uuid.UUID]


class PublicBusiness(Schema):
    name: str
    slug: str
    timezone: str
    currency: str
    profile: dict[str, Any]
    locations: list[PublicLocation]
    services: list[PublicService]
    providers: list[PublicProvider]


class PublicBookingIn(Schema):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=6, max_length=32)
    email: EmailStr | None = None
    service_id: uuid.UUID
    location_id: uuid.UUID
    provider_id: uuid.UUID | None = None
    starts_at: datetime
    notes: str | None = Field(default=None, max_length=1000)
    whatsapp_opt_in: bool = False


class PublicBookingOut(Schema):
    appointment_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    location: str
    service: str
    provider: str
    pay_url: str | None


class PublicPayment(Schema):
    business_name: str
    number: str
    amount_minor: int
    currency: str
    purpose: str
    description: str | None
    lines: list[InvoiceLine]
    status: str
    customer_name: str | None
    due_at: datetime | None
    paid_at: datetime | None
    provider: str
    checkout_url: str | None


class TestConfirmIn(Schema):
    method: str = Field(default="upi", pattern="^(upi|card|netbanking)$")


class WebchatSessionIn(Schema):
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    page_url: str | None = Field(default=None, max_length=2000)


class WebchatSessionOut(Schema):
    token: str
    conversation_id: uuid.UUID
    business_name: str
    agent_name: str


class WebchatMessageIn(Schema):
    body: str = Field(min_length=1, max_length=4000)
    client_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _strip(self) -> "WebchatMessageIn":
        self.body = self.body.strip()
        if not self.body:
            raise ValueError("Message is empty")
        return self


class WebchatMessage(Schema):
    id: uuid.UUID
    author: str
    author_name: str | None
    body: str
    card: dict[str, Any] | None
    created_at: datetime


class FeedbackPage(Schema):
    business_name: str
    first_name: str | None
    status: str
    rating: int | None
    issues: list[str]
    public_link: str | None
    suggestions: dict[str, str]


class FeedbackRateIn(Schema):
    rating: int = Field(ge=1, le=5)


class FeedbackRateOut(Schema):
    public_link: str | None
    suggestions: dict[str, str]


class FeedbackPrivateIn(Schema):
    issues: list[str] = Field(default_factory=list, max_length=10)
    text: str | None = Field(default=None, max_length=4000)


class LandingPublic(Schema):
    business_name: str
    name: str
    variant: str
    sections: list[dict[str, Any]]


class LandingSubmitIn(Schema):
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    message: str | None = Field(default=None, max_length=2000)
    variant: str = "A"
    marketing_consent: bool = False
    utm: dict[str, str] = Field(default_factory=dict)


class LandingSubmitOut(Schema):
    ok: bool
    message: str

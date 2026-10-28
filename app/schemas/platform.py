import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, HttpUrl

from app.schemas.common import Schema

API_SCOPES = [
    "contacts:read",
    "contacts:write",
    "conversations:read",
    "conversations:write",
    "appointments:read",
    "appointments:write",
    "payments:read",
    "payments:write",
    "tasks:read",
    "tasks:write",
    "deals:read",
    "deals:write",
    "campaigns:read",
    "campaigns:write",
    "reviews:read",
    "reviews:write",
    "*",
]


class IntegrationOut(Schema):
    id: str
    name: str
    category: str
    description: str
    color: str
    permissions: list[str]
    popular: bool
    auth: str
    connected: bool
    account: str | None
    health: str | None
    last_sync_at: datetime | None
    last_error: str | None
    fields: list[str]
    platform_provided: bool | None
    oauth_env: list[str]


class ConnectIn(Schema):
    account: str | None = Field(default=None, max_length=200)
    credentials: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class ProviderStatusOut(Schema):
    id: str
    name: str
    kind: str
    paid: bool
    env: list[str]
    configured: bool
    docs: str


class ApiKeyIn(Schema):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1)


class ApiKeyOut(Schema):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    # Shown once, at creation.
    key: str | None = None


class WebhookEndpointIn(Schema):
    url: HttpUrl
    events: list[str] = Field(min_length=1)
    active: bool = True


class WebhookEndpointOut(Schema):
    id: uuid.UUID
    url: str
    events: list[str]
    active: bool
    success_count: int
    failure_count: int
    last_delivery_at: datetime | None
    last_status: int | None
    last_error: str | None
    created_at: datetime
    secret: str | None = None  # shown once, at creation


class PlanOut(Schema):
    id: str
    name: str
    price_minor: int
    who: str
    features: list[str]


class MeterOut(Schema):
    id: str
    label: str
    used: int
    limit: int
    unit: str
    resets_at: datetime


class InvoiceOut(Schema):
    id: uuid.UUID
    number: str
    amount_minor: int
    currency: str
    status: str
    items: str
    issued_at: datetime


class CreditPackOut(Schema):
    id: str
    label: str
    price_minor: int


class BillingOut(Schema):
    plan: str
    plans: list[PlanOut]
    meters: list[MeterOut]
    credit_packs: list[CreditPackOut]
    invoices: list[InvoiceOut]
    checkout_provider_configured: bool


class ChangePlanIn(Schema):
    plan: str


class BuyCreditsIn(Schema):
    pack: str


class CheckoutOut(Schema):
    applied: bool
    checkout_url: str | None = None


class DataRequestIn(Schema):
    type: Literal["export", "delete"]
    subject: str = Field(min_length=2, max_length=200)
    note: str | None = None


class DataRequestOut(Schema):
    id: uuid.UUID
    number: str
    type: str
    subject: str
    contact_id: uuid.UUID | None
    status: str
    due_at: datetime
    completed_at: datetime | None
    note: str | None
    created_at: datetime


class NotificationPrefs(Schema):
    preferences: dict[str, dict[str, bool]]

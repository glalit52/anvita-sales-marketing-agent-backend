import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import Schema

PaymentStatus = Literal["pending", "paid", "overdue", "refunded", "cancelled", "expired"]


class InvoiceLine(Schema):
    description: str = Field(max_length=300)
    quantity: int = Field(default=1, ge=1)
    unit_minor: int = Field(ge=0)
    tax_pct: float = Field(default=0, ge=0, le=100)


class PaymentIn(Schema):
    contact_id: uuid.UUID | None = None
    appointment_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    # Either an amount, or invoice lines from which the amount is computed.
    amount_minor: int | None = Field(default=None, gt=0)
    lines: list[InvoiceLine] = Field(default_factory=list)
    purpose: str = Field(max_length=200)
    description: str | None = None
    due_at: datetime | None = None

    @model_validator(mode="after")
    def _amount_or_lines(self) -> "PaymentIn":
        if self.amount_minor is None and not self.lines:
            raise ValueError("Provide amountMinor or at least one invoice line")
        return self


class PaymentOut(Schema):
    id: uuid.UUID
    number: str
    contact_id: uuid.UUID | None
    appointment_id: uuid.UUID | None
    deal_id: uuid.UUID | None
    amount_minor: int
    currency: str
    purpose: str
    description: str | None
    lines: list[InvoiceLine]
    status: PaymentStatus
    method: str | None
    provider: str
    created_by_type: str
    due_at: datetime | None
    paid_at: datetime | None
    refunded_at: datetime | None
    created_at: datetime
    pay_url: str


class PaymentSummary(Schema):
    collected_minor: int
    pending_minor: int
    overdue_minor: int
    refunded_minor: int
    paid_count: int
    pending_count: int

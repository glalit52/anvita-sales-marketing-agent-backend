import base64
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import constant_time_eq, hmac_sha256_hex
from app.db.base import utcnow
from app.models import Contact, Organization, Payment
from app.services.events import emit
from app.services.notify import notify

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderOrder:
    order_id: str
    checkout_url: str | None


class PaymentProvider(Protocol):
    name: str

    async def create_order(self, payment: Payment, contact: Contact | None) -> ProviderOrder: ...


class TestPaymentProvider:
    """Hosted on our own /pay page; paid via the public test-confirm endpoint. Never allowed in production."""

    name = "test"

    async def create_order(self, payment: Payment, contact: Contact | None) -> ProviderOrder:
        return ProviderOrder(order_id=f"test_{secrets.token_hex(8)}", checkout_url=None)


class RazorpayProvider:
    name = "razorpay"
    base_url = "https://api.razorpay.com/v1"

    async def create_order(self, payment: Payment, contact: Contact | None) -> ProviderOrder:
        s = get_settings()
        if not s.razorpay_key_id or not s.razorpay_key_secret:
            raise AppError("Razorpay is not configured", code="payments_not_configured")
        auth = base64.b64encode(f"{s.razorpay_key_id}:{s.razorpay_key_secret.get_secret_value()}".encode()).decode()
        body: dict = {
            "amount": payment.amount_minor,
            "currency": payment.currency,
            "description": payment.purpose[:255],
            "reference_id": str(payment.id),
            "callback_url": f"{s.app_base_url}/pay/{payment.public_token}?status=done",
            "callback_method": "get",
            "notify": {"sms": False, "email": False},
        }
        if contact:
            body["customer"] = {
                k: v for k, v in {"name": contact.name, "email": contact.email, "contact": contact.phone}.items() if v
            }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/payment_links", json=body, headers={"Authorization": f"Basic {auth}"}
            )
        if resp.status_code >= 400:
            log.warning("razorpay create failed", extra={"data": {"status": resp.status_code, "body": resp.text[:300]}})
            raise AppError("Payment provider rejected the request", code="payment_provider_error")
        data = resp.json()
        return ProviderOrder(order_id=data["id"], checkout_url=data.get("short_url"))


def provider() -> PaymentProvider:
    return RazorpayProvider() if get_settings().payments_provider == "razorpay" else TestPaymentProvider()


def verify_razorpay_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = get_settings().razorpay_webhook_secret
    if secret is None or not signature:
        return False
    return constant_time_eq(signature, hmac_sha256_hex(secret.get_secret_value(), raw_body))


def pay_url(payment: Payment) -> str:
    return f"{get_settings().app_base_url}/pay/{payment.public_token}"


def compute_amount(lines: list[dict]) -> int:
    total = 0
    for line in lines:
        sub = line["quantity"] * line["unitMinor"]
        total += sub + round(sub * line.get("taxPct", 0) / 100)
    return total


async def create_payment(
    db: AsyncSession,
    org: Organization,
    *,
    amount_minor: int | None,
    purpose: str,
    contact: Contact | None = None,
    lines: list[dict] | None = None,
    description: str | None = None,
    appointment_id: uuid.UUID | None = None,
    deal_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    due_at: datetime | None = None,
    created_by_type: str = "user",
    created_by_id: uuid.UUID | None = None,
) -> Payment:
    lines = lines or []
    amount = amount_minor if amount_minor is not None else compute_amount(lines)
    if amount <= 0:
        raise AppError("Amount must be positive", code="invalid_amount")
    now = utcnow()
    payment = Payment(
        id=uuid.uuid4(),
        org_id=org.id,
        contact_id=contact.id if contact else None,
        appointment_id=appointment_id,
        deal_id=deal_id,
        conversation_id=conversation_id,
        number=f"INV-{now:%Y%m}-{secrets.token_hex(3).upper()}",
        amount_minor=amount,
        currency=org.currency,
        purpose=purpose,
        description=description,
        lines=lines,
        status="pending",
        public_token=secrets.token_urlsafe(24),
        created_by_type=created_by_type,
        created_by_id=created_by_id,
        due_at=due_at,
    )
    prov = provider()
    order = await prov.create_order(payment, contact)
    payment.provider = prov.name
    payment.provider_order_id = order.order_id
    payment.checkout_url = order.checkout_url
    db.add(payment)
    await db.flush()
    return payment


async def mark_paid(db: AsyncSession, payment: Payment, *, method: str | None, provider_payment_id: str | None) -> bool:
    """Idempotent: provider webhooks are retried, so a second 'paid' for the same payment is a no-op."""
    if payment.status == "paid":
        return False
    payment.status = "paid"
    payment.method = method
    payment.provider_payment_id = provider_payment_id
    payment.paid_at = utcnow()
    amount = f"{payment.currency} {payment.amount_minor / 100:,.2f}"
    notify(
        db,
        payment.org_id,
        tab="system",
        icon="payment",
        title=f"Payment received · {amount}",
        body=payment.purpose,
        contact_id=payment.contact_id,
        link=f"/payments?id={payment.id}",
    )
    emit(db, payment.org_id, "payment.paid", {"payment_id": payment.id})
    return True

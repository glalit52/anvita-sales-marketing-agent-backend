"""Outgoing webhooks: signed POSTs of domain events to customer endpoints, delivered by the worker."""

import json
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hmac_sha256_hex
from app.db.base import utcnow
from app.models import WebhookEndpoint
from app.services.events import emit
from app.services.knowledge import UnsafeURL, assert_public_url

# Internal event type → public event name.
PUBLIC_EVENTS = {
    "contact.created": "contact.created",
    "contact.updated": "contact.updated",
    "conversation.escalated": "conversation.escalated",
    "message.received": "message.received",
    "appointment.booked": "appointment.booked",
    "appointment.cancelled": "appointment.cancelled",
    "payment.paid": "payment.received",
    "review.received": "review.received",
    "agent_run.completed": "agent_run.completed",
    "campaign.sent": "campaign.sent",
    "prospect_search.completed": "prospect_search.completed",
}


async def fanout(
    db: AsyncSession, org_id: uuid.UUID, internal_type: str, payload: dict[str, Any], event_id: uuid.UUID
) -> int:
    public = PUBLIC_EVENTS.get(internal_type)
    if not public:
        return 0
    endpoints = (
        await db.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.org_id == org_id,
                WebhookEndpoint.active.is_(True),
                WebhookEndpoint.events.contains([public]),
            )
        )
    ).all()  # type: ignore[arg-type]
    for ep in endpoints:
        emit(
            db,
            org_id,
            "webhook.deliver",
            {"endpoint_id": ep.id, "event": public, "event_id": str(event_id), "data": payload},
        )
    return len(endpoints)


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac_sha256_hex(secret, timestamp.encode() + b"." + body)


async def deliver(
    db: AsyncSession,
    endpoint: WebhookEndpoint,
    event: str,
    event_id: str,
    data: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> bool:
    body = json.dumps(
        {"id": event_id, "type": event, "createdAt": utcnow().isoformat(), "data": data}, default=str
    ).encode()
    ts = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "x-anvita-event": event,
        "x-anvita-delivery": event_id,
        "x-anvita-timestamp": ts,
        "x-anvita-signature": signature(endpoint.secret, ts, body),
        "user-agent": "Anvita-Webhooks/1.0",
    }
    endpoint.last_delivery_at = utcnow()
    try:
        await assert_public_url(endpoint.url)
        owns = client is None
        client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        try:
            resp = await client.post(endpoint.url, content=body, headers=headers)
        finally:
            if owns:
                await client.aclose()
        endpoint.last_status = resp.status_code
        ok = 200 <= resp.status_code < 300
        endpoint.last_error = None if ok else f"HTTP {resp.status_code}"
    except (httpx.HTTPError, UnsafeURL) as e:
        endpoint.last_status, endpoint.last_error, ok = None, str(e)[:500], False
    if ok:
        endpoint.success_count += 1
    else:
        endpoint.failure_count += 1
    return ok

"""Provider webhooks. Each handler verifies the signature, stores what arrived, and acks fast; slow work
(AI replies, sends) happens in the worker."""

import json
import logging

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.errors import Forbidden, Unauthorized
from app.core.security import constant_time_eq
from app.db.session import tenant_session
from app.models import Channel, Message, Organization, Payment
from app.services.channels.whatsapp import parse_webhook, verify_signature
from app.services.contacts import get_or_create_contact
from app.services.inbox import record_inbound
from app.services.payments import mark_paid, verify_razorpay_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp")
async def whatsapp_verify(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge"),
) -> Response:
    expected = get_settings().whatsapp_verify_token
    if mode != "subscribe" or expected is None or not constant_time_eq(token, expected.get_secret_value()):
        raise Forbidden("Verification failed")
    return Response(content=challenge, media_type="text/plain")


@router.post("/whatsapp")
async def whatsapp_inbound(request: Request) -> dict:
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("x-hub-signature-256")):
        raise Unauthorized("Bad signature")
    inbound, statuses = parse_webhook(json.loads(raw or b"{}"))

    # Route by phone_number_id → channel → tenant. Lookup spans tenants, so it runs with RLS bypass.
    phone_ids = {m.phone_number_id for m in inbound}
    routes: dict[str, tuple] = {}
    if phone_ids:
        async with tenant_session(bypass_rls=True) as db:
            rows = await db.execute(
                select(Channel.external_id, Channel.id, Channel.org_id).where(
                    Channel.type == "whatsapp", Channel.external_id.in_(phone_ids)
                )
            )
            routes = {ext: (cid, oid) for ext, cid, oid in rows}

    for m in inbound:
        route = routes.get(m.phone_number_id)
        if route is None:
            log.warning("whatsapp message for unknown number", extra={"data": {"phone_number_id": m.phone_number_id}})
            continue
        channel_id, org_id = route
        async with tenant_session(org_id) as db:
            org = await db.get(Organization, org_id)
            assert org is not None
            contact, _ = await get_or_create_contact(
                db,
                org_id,
                name=m.profile_name,
                phone=f"+{m.wa_id}",
                channel="whatsapp",
                source="WhatsApp",
                country=org.country,
            )
            contact.whatsapp_id = m.wa_id
            await record_inbound(
                db,
                org_id,
                contact,
                channel_type="whatsapp",
                channel_id=channel_id,
                body=m.body,
                provider_msg_id=m.provider_msg_id,
            )
            await db.commit()

    if statuses:
        rank = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
        async with tenant_session(bypass_rls=True) as db:
            for s in statuses:
                if s.status not in rank:
                    continue
                q = update(Message).where(Message.provider_msg_id == s.provider_msg_id)
                if s.status != "failed":  # statuses can arrive out of order; never move backwards
                    q = q.where(Message.status.notin_([k for k, v in rank.items() if v >= rank[s.status]]))
                await db.execute(q.values(status=s.status, error=s.error))
            await db.commit()
    return {"ok": True}


@router.post("/razorpay")
async def razorpay(request: Request) -> dict:
    raw = await request.body()
    if not verify_razorpay_signature(raw, request.headers.get("x-razorpay-signature")):
        raise Unauthorized("Bad signature")
    event = json.loads(raw)
    if event.get("event") not in ("payment_link.paid", "payment.captured"):
        return {"ok": True, "ignored": event.get("event")}
    payload = event.get("payload", {})
    link = payload.get("payment_link", {}).get("entity", {})
    pay = payload.get("payment", {}).get("entity", {})
    order_id = link.get("id")
    if not order_id:
        return {"ok": True, "ignored": "no payment link id"}
    async with tenant_session(bypass_rls=True) as db:
        found = (
            await db.execute(select(Payment.id, Payment.org_id).where(Payment.provider_order_id == order_id))
        ).first()
    if found is None:
        log.warning("razorpay webhook for unknown link", extra={"data": {"order_id": order_id}})
        return {"ok": True, "ignored": "unknown link"}
    async with tenant_session(found.org_id) as db:
        payment = await db.get(Payment, found.id, with_for_update=True)
        assert payment is not None
        await mark_paid(db, payment, method=pay.get("method"), provider_payment_id=pay.get("id"))
        await db.commit()
    return {"ok": True}

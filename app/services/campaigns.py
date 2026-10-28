"""Broadcast campaigns: resolve the segment, respect consent, send through the channel, track outcomes."""

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.base import utcnow
from app.models import (
    Appointment,
    Campaign,
    CampaignRecipient,
    Contact,
    Message,
    MessageTemplate,
    Organization,
    Payment,
    Segment,
)
from app.services import segments
from app.services.events import emit
from app.services.inbox import create_outbound, open_conversation, window_open
from app.services.templating import contact_vars, positional, render

ATTRIBUTION_DAYS = 14
BATCH = 200


async def audience(db: AsyncSession, campaign: Campaign) -> tuple[int, int]:
    """(eligible with consent, total in segment)."""
    seg = await db.get(Segment, campaign.segment_id) if campaign.segment_id else None
    rules = seg.rules if seg else {}
    base = segments.where(campaign.org_id, rules)
    total = int(await db.scalar(select(func.count()).select_from(Contact).where(base)) or 0)
    eligible = int(
        await db.scalar(
            select(func.count())
            .select_from(Contact)
            .where(base, segments.has_consent(campaign.channel), *_reachable(campaign.channel))
        )
        or 0
    )
    return eligible, total


def _reachable(channel: str) -> list[Any]:
    return [Contact.email.is_not(None)] if channel == "email" else [Contact.phone.is_not(None)]


async def start(db: AsyncSession, campaign: Campaign) -> None:
    if campaign.status not in ("draft", "scheduled"):
        raise AppError(f"Campaign is {campaign.status}", code="invalid_status")
    if campaign.channel == "whatsapp":
        tpl = await db.get(MessageTemplate, campaign.template_id) if campaign.template_id else None
        if tpl is None or tpl.status != "approved":
            raise AppError("WhatsApp broadcasts need an approved template", code="template_not_approved")
    eligible, total = await audience(db, campaign)
    campaign.audience_count, campaign.skipped_no_consent = eligible, total - eligible
    now = utcnow()
    delay = max(0.0, (campaign.scheduled_at - now).total_seconds()) if campaign.scheduled_at else 0.0
    campaign.status = "scheduled" if delay > 0 else "sending"
    emit(db, campaign.org_id, "campaign.send", {"campaign_id": campaign.id}, delay_seconds=delay)


async def send_batch(db: AsyncSession, campaign: Campaign) -> bool:
    """Send up to BATCH messages; returns True when more remain (the worker re-queues itself)."""
    if campaign.status == "cancelled":
        return False
    org = await db.get(Organization, campaign.org_id)
    assert org is not None
    campaign.status = "sending"
    campaign.started_at = campaign.started_at or utcnow()
    seg = await db.get(Segment, campaign.segment_id) if campaign.segment_id else None
    tpl = await db.get(MessageTemplate, campaign.template_id) if campaign.template_id else None
    already = select(CampaignRecipient.contact_id).where(CampaignRecipient.campaign_id == campaign.id)
    batch = (
        await db.scalars(
            select(Contact)
            .where(
                segments.where(campaign.org_id, seg.rules if seg else {}),
                segments.has_consent(campaign.channel),
                *_reachable(campaign.channel),
                Contact.id.notin_(already),
            )
            .order_by(Contact.id)
            .limit(BATCH)
        )
    ).all()
    body = tpl.body if tpl else campaign.body
    for contact in batch:
        values = contact_vars(contact, {"business": org.name})
        rec = CampaignRecipient(org_id=org.id, campaign_id=campaign.id, contact_id=contact.id)
        db.add(rec)
        conv = await open_conversation(db, org.id, contact, campaign.channel, None)
        template = None
        if campaign.channel == "whatsapp" and tpl and not window_open(conv):
            names = positional(tpl.body)
            template = {
                "name": tpl.provider_template_id or tpl.name,
                "language": "en",
                "params": [values.get(n, "") if not n.isdigit() else _numbered(values, int(n)) for n in names],
            }
        try:
            msg = await create_outbound(
                db,
                conv,
                body=render(body, values),
                author_type="human",
                author_name=f"Campaign · {campaign.name}",
                pause_ai=False,
                template=template,
                idempotency_key=f"campaign:{campaign.id}:{contact.id}",
            )
            rec.conversation_id, rec.message_id, rec.status, rec.sent_at = conv.id, msg.id, "sent", utcnow()
        except AppError as e:
            rec.status, rec.error = "failed", e.message[:300]
    if len(batch) < BATCH:
        campaign.status, campaign.sent_at = "sent", utcnow()
        emit(
            db,
            org.id,
            "campaign.sent",
            {"campaign_id": campaign.id, "name": campaign.name, "audience": campaign.audience_count},
        )
        return False
    return True


def _numbered(values: dict[str, str], n: int) -> str:
    # WhatsApp {{1}}, {{2}}, {{3}}: first name, business name, then free.
    return {1: values.get("first_name", ""), 2: values.get("business", "")}.get(n, "")


async def stats(db: AsyncSession, campaign: Campaign) -> dict[str, int]:
    rows = (
        await db.execute(
            select(CampaignRecipient.status, Message.status, func.count())
            .outerjoin(Message, Message.id == CampaignRecipient.message_id)
            .where(CampaignRecipient.campaign_id == campaign.id)
            .group_by(CampaignRecipient.status, Message.status)
        )
    ).all()
    out = {"sent": 0, "delivered": 0, "read": 0, "failed": 0}
    for rec_status, msg_status, n in rows:
        if rec_status == "failed" or msg_status == "failed":
            out["failed"] += n
            continue
        if rec_status == "sent":
            out["sent"] += n
        if msg_status in ("delivered", "read"):
            out["delivered"] += n
        if msg_status == "read":
            out["read"] += n
    start = campaign.started_at or campaign.created_at
    end = start + timedelta(days=ATTRIBUTION_DAYS)
    contact_ids = select(CampaignRecipient.contact_id).where(
        CampaignRecipient.campaign_id == campaign.id, CampaignRecipient.status == "sent"
    )
    conv_ids = select(CampaignRecipient.conversation_id).where(CampaignRecipient.campaign_id == campaign.id)
    out["replied"] = int(
        await db.scalar(
            select(func.count(func.distinct(Message.conversation_id))).where(
                Message.conversation_id.in_(conv_ids), Message.author_type == "contact", Message.created_at >= start
            )
        )
        or 0
    )
    out["booked"] = int(
        await db.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.contact_id.in_(contact_ids),
                and_(Appointment.created_at >= start, Appointment.created_at < end),
            )
        )
        or 0
    )
    out["revenue_minor"] = int(
        await db.scalar(
            select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
                Payment.contact_id.in_(contact_ids),
                Payment.status == "paid",
                Payment.paid_at >= start,
                Payment.paid_at < end,
            )
        )
        or 0
    )
    return out

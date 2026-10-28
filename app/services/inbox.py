import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict
from app.db.base import utcnow
from app.models import Contact, Conversation, Message
from app.services.channels import adapter_for
from app.services.events import emit


def _preview(body: str) -> str:
    body = " ".join(body.split())
    return body if len(body) <= 140 else body[:137] + "…"


async def open_conversation(
    db: AsyncSession, org_id: uuid.UUID, contact: Contact, channel_type: str, channel_id: uuid.UUID | None
) -> Conversation:
    """The contact's current thread on this channel; a closed one is reopened rather than duplicated."""
    conv = await db.scalar(
        select(Conversation)
        .where(Conversation.contact_id == contact.id, Conversation.channel_type == channel_type)
        .order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc())
        .limit(1)
    )
    if conv is None:
        conv = Conversation(
            org_id=org_id,
            contact_id=contact.id,
            channel_id=channel_id,
            channel_type=channel_type,
            status="open",
            assignee_type="ai" if contact.owner_id is None else "human",
            assignee_id=contact.owner_id,
            unread_count=0,
        )
        db.add(conv)
        await db.flush()
    elif conv.status == "closed":
        conv.status = "open"
    if channel_id and conv.channel_id is None:
        conv.channel_id = channel_id
    return conv


async def record_inbound(
    db: AsyncSession,
    org_id: uuid.UUID,
    contact: Contact,
    *,
    channel_type: str,
    channel_id: uuid.UUID | None,
    body: str,
    provider_msg_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[Conversation, Message | None]:
    """Store an inbound message and wake the agent. Idempotent on the provider message id, because
    providers retry webhooks. Returns (conversation, None) for a duplicate."""
    if provider_msg_id:
        dup = await db.scalar(
            select(Message).where(Message.org_id == org_id, Message.provider_msg_id == provider_msg_id)
        )
        if dup:
            conv = await db.get(Conversation, dup.conversation_id)
            assert conv is not None
            return conv, None

    conv = await open_conversation(db, org_id, contact, channel_type, channel_id)
    now = utcnow()
    msg = Message(
        org_id=org_id,
        conversation_id=conv.id,
        direction="inbound",
        author_type="contact",
        author_name=contact.name,
        body=body,
        provider_msg_id=provider_msg_id,
        attachments=attachments or [],
        sources=[],
        status="received",
        created_at=now,
    )
    db.add(msg)
    conv.last_message_at = now
    conv.last_inbound_at = now
    conv.last_message_preview = _preview(body)
    conv.unread_count = (conv.unread_count or 0) + 1
    contact.last_activity_at = now
    await db.flush()
    emit(
        db,
        org_id,
        "message.received",
        {"conversation_id": conv.id, "message_id": msg.id},
        delay_seconds=get_settings().agent_debounce_seconds,
    )
    return conv, msg


async def create_outbound(
    db: AsyncSession,
    conv: Conversation,
    *,
    body: str,
    author_type: str,
    author_id: uuid.UUID | None = None,
    author_name: str | None = None,
    is_note: bool = False,
    is_draft: bool = False,
    card: dict[str, Any] | None = None,
    sources: list[str] | None = None,
    ai_run_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    template: dict[str, Any] | None = None,
    pause_ai: bool = True,
) -> Message:
    """Write a team/AI/system message. Real sends are queued to the outbox and delivered by the worker.

    `template` marks a pre-approved template send ({"name", "language", "params"}), which channels with a
    reply window (WhatsApp) allow outside it. Automated sends (campaigns, sequences) pass pause_ai=False so
    they don't silence the AI the way a teammate's reply does."""
    if idempotency_key:
        existing = await db.scalar(
            select(Message).where(Message.org_id == conv.org_id, Message.idempotency_key == idempotency_key)
        )
        if existing:
            return existing

    deliver = not is_note and not is_draft and author_type != "system"
    if deliver:
        window = adapter_for(conv.channel_type).capabilities.window_hours
        window_closed = conv.last_inbound_at is None or utcnow() - conv.last_inbound_at > timedelta(hours=window or 0)
        if window and template is None and window_closed:
            raise Conflict(
                f"The {window}h reply window on {conv.channel_type} has closed; send an approved template",
                code="reply_window_closed",
            )

    now = utcnow()
    msg = Message(
        org_id=conv.org_id,
        conversation_id=conv.id,
        direction="internal" if (is_note or is_draft) else "outbound",
        author_type=author_type,
        author_id=author_id,
        author_name=author_name,
        body=body,
        is_note=is_note,
        is_draft=is_draft,
        card=card,
        sources=sources or [],
        attachments=[],
        status="queued" if deliver else "sent",
        ai_run_id=ai_run_id,
        idempotency_key=idempotency_key,
        created_at=now,
    )
    db.add(msg)
    if not is_draft:
        conv.last_message_at = now
        conv.last_message_preview = _preview(body)
    if template:
        msg.attachments = [{"kind": "template", **template}]
    if author_type == "human" and not is_note and pause_ai:
        # Human in control: a teammate replying pauses the AI on this thread for a while.
        conv.ai_paused_until = now + timedelta(minutes=get_settings().agent_human_pause_minutes)
        conv.unread_count = 0
    await db.flush()
    if deliver:
        emit(db, conv.org_id, "message.outbound", {"message_id": msg.id})
    return msg


def window_open(conv: Conversation) -> bool:
    """Whether a free-form message is allowed right now on this conversation's channel."""
    window = adapter_for(conv.channel_type).capabilities.window_hours
    if not window:
        return True
    return conv.last_inbound_at is not None and utcnow() - conv.last_inbound_at <= timedelta(hours=window)

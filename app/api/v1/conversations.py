import uuid
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DB, CurrentPrincipal
from app.core.errors import AppError, NotFound
from app.db.base import utcnow
from app.models import Contact, Conversation, Membership, Message
from app.schemas.common import Page
from app.schemas.crm import ContactOut
from app.schemas.inbox import (
    ConversationDetail,
    ConversationListItem,
    ConversationUpdate,
    InboxCounts,
    MessageOut,
    SendMessageIn,
    StartConversationIn,
    SuggestedReply,
)
from app.services.agent.runtime import suggest_reply
from app.services.audit import audit
from app.services.events import emit
from app.services.inbox import create_outbound, open_conversation

router = APIRouter(prefix="/conversations", tags=["inbox"])


async def _get(db: DB, principal: CurrentPrincipal, conversation_id: uuid.UUID, *, lock: bool = False) -> Conversation:
    conv = await db.get(Conversation, conversation_id, with_for_update=lock)
    if conv is None or conv.org_id != principal.org_id:
        raise NotFound("Conversation not found")
    return conv


def _fields(conv: Conversation, contact: Contact) -> dict:
    return {
        **{c.key: getattr(conv, c.key) for c in Conversation.__table__.columns},
        "contact": ContactOut.model_validate(contact),
    }


def _item(conv: Conversation, contact: Contact) -> ConversationListItem:
    return ConversationListItem.model_validate(_fields(conv, contact))


@router.get("", response_model=Page[ConversationListItem])
async def list_conversations(
    principal: CurrentPrincipal,
    db: DB,
    view: Literal["all", "mine", "ai", "escalated", "unassigned"] = "all",
    status: list[Literal["open", "pending", "closed"]] | None = Query(None),
    channel: str | None = None,
    q: str | None = None,
    unread: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ConversationListItem]:
    cond = [Conversation.org_id == principal.org_id]
    if view == "mine":
        cond.append(Conversation.assignee_id == principal.user_id)
    elif view == "ai":
        cond.append(Conversation.assignee_type == "ai")
    elif view == "escalated":
        cond += [Conversation.escalation_reason.is_not(None), Conversation.status != "closed"]
    elif view == "unassigned":
        cond += [Conversation.assignee_type == "human", Conversation.assignee_id.is_(None)]
    if status:
        cond.append(Conversation.status.in_(status))
    if channel:
        cond.append(Conversation.channel_type == channel)
    if unread:
        cond.append(Conversation.unread_count > 0)
    if q:
        like = f"%{q.strip()}%"
        cond.append(Contact.name.ilike(like) | Conversation.last_message_preview.ilike(like))
    base = select(Conversation, Contact).join(Contact, Contact.id == Conversation.contact_id).where(*cond)
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = await db.execute(
        base.order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.id).limit(limit).offset(offset)
    )
    return Page(items=[_item(c, ct) for c, ct in rows], total=total, limit=limit, offset=offset)


@router.get("/counts", response_model=InboxCounts)
async def counts(principal: CurrentPrincipal, db: DB) -> InboxCounts:
    open_ = (Conversation.org_id == principal.org_id) & (Conversation.status != "closed")
    row = (
        await db.execute(
            select(
                func.count().filter(open_),
                func.count().filter(open_ & (Conversation.assignee_id == principal.user_id)),
                func.count().filter(open_ & (Conversation.assignee_type == "ai")),
                func.count().filter(open_ & Conversation.escalation_reason.is_not(None)),
                func.count().filter(open_ & (Conversation.unread_count > 0)),
            ).select_from(Conversation)
        )
    ).one()
    return InboxCounts(all=row[0], mine=row[1], ai=row[2], escalated=row[3], unread=row[4])


@router.post("", response_model=ConversationDetail, status_code=201)
async def start_conversation(body: StartConversationIn, principal: CurrentPrincipal, db: DB) -> ConversationDetail:
    """Team-initiated outreach to an existing contact."""
    contact = await db.get(Contact, body.contact_id)
    if contact is None or contact.org_id != principal.org_id:
        raise NotFound("Contact not found")
    conv = await open_conversation(db, principal.org_id, contact, body.channel_type, None)
    conv.assignee_type, conv.assignee_id = "human", principal.user_id
    await create_outbound(
        db, conv, body=body.body, author_type="human", author_id=principal.user_id, author_name=principal.user.name
    )
    await db.commit()
    return await get_conversation(conv.id, principal, db, limit=200)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, principal: CurrentPrincipal, db: DB, limit: int = Query(200, le=1000)
) -> ConversationDetail:
    conv = await _get(db, principal, conversation_id)
    contact = await db.get(Contact, conv.contact_id)
    assert contact is not None
    msgs = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(limit)
            )
        ).all()
    )[::-1]
    return ConversationDetail.model_validate(
        {**_fields(conv, contact), "messages": [MessageOut.model_validate(m) for m in msgs]}
    )


@router.post("/{conversation_id}/read", response_model=ConversationListItem)
async def mark_read(conversation_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> ConversationListItem:
    conv = await _get(db, principal, conversation_id)
    conv.unread_count = 0
    await db.commit()
    contact = await db.get(Contact, conv.contact_id)
    assert contact is not None
    return _item(conv, contact)


@router.patch("/{conversation_id}", response_model=ConversationListItem)
async def update_conversation(
    conversation_id: uuid.UUID, body: ConversationUpdate, principal: CurrentPrincipal, db: DB
) -> ConversationListItem:
    conv = await _get(db, principal, conversation_id, lock=True)
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    if "status" in changes:
        conv.status = changes["status"]
        if conv.status == "closed":
            conv.escalation_reason = None
    if changes.get("assignee_type") == "ai":
        conv.assignee_type, conv.assignee_id, conv.escalation_reason, conv.ai_paused_until = "ai", None, None, None
        await create_outbound(
            db, conv, body=f"{principal.user.name} handed the conversation back to the AI", author_type="system"
        )
    elif "assignee_id" in changes:
        if changes["assignee_id"] is not None:
            member = await db.scalar(
                select(Membership.id).where(
                    Membership.org_id == principal.org_id, Membership.user_id == changes["assignee_id"]
                )
            )
            if member is None:
                raise AppError("Assignee is not a member of this workspace", code="invalid_assignee")
        conv.assignee_type, conv.assignee_id = "human", changes["assignee_id"]
    if "ai_paused_until" in changes:
        conv.ai_paused_until = changes["ai_paused_until"]
    if changes.get("resolve_escalation"):
        conv.escalation_reason = None
    audit(
        db,
        principal.org_id,
        "conversation.updated",
        actor_id=principal.user_id,
        entity_type="conversation",
        entity_id=conv.id,
        data=body.model_dump(mode="json", exclude_unset=True),
    )
    await db.commit()
    contact = await db.get(Contact, conv.contact_id)
    assert contact is not None
    return _item(conv, contact)


@router.post("/{conversation_id}/messages", response_model=MessageOut, status_code=201)
async def send_message(
    conversation_id: uuid.UUID, body: SendMessageIn, principal: CurrentPrincipal, db: DB
) -> MessageOut:
    """Reply as a teammate, or add an internal note (`isNote`). Replying pauses the AI on the thread."""
    conv = await _get(db, principal, conversation_id, lock=True)
    msg = await create_outbound(
        db,
        conv,
        body=body.body,
        author_type="human",
        author_id=principal.user_id,
        author_name=principal.user.name,
        is_note=body.is_note,
        idempotency_key=body.idempotency_key,
    )
    if not body.is_note and conv.assignee_type == "ai":
        conv.assignee_type, conv.assignee_id = "human", principal.user_id
    await db.commit()
    return MessageOut.model_validate(msg)


@router.post("/{conversation_id}/drafts/{message_id}/send", response_model=MessageOut)
async def send_draft(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DB,
    body: SendMessageIn | None = None,
) -> MessageOut:
    """Copilot mode: approve (optionally edit) an AI draft and send it."""
    conv = await _get(db, principal, conversation_id, lock=True)
    draft = await db.get(Message, message_id, with_for_update=True)
    if draft is None or draft.conversation_id != conv.id or not draft.is_draft:
        raise NotFound("Draft not found")
    msg = await create_outbound(
        db,
        conv,
        body=body.body if body else draft.body,
        author_type="ai",
        author_name=draft.author_name,
        card=draft.card,
        sources=draft.sources,
        ai_run_id=draft.ai_run_id,
        idempotency_key=f"draft:{draft.id}",
    )
    await db.delete(draft)
    audit(
        db,
        principal.org_id,
        "draft.approved",
        actor_id=principal.user_id,
        entity_type="message",
        entity_id=msg.id,
        data={"edited": body is not None},
    )
    await db.commit()
    return MessageOut.model_validate(msg)


@router.delete("/{conversation_id}/drafts/{message_id}", status_code=204)
async def discard_draft(conversation_id: uuid.UUID, message_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> None:
    conv = await _get(db, principal, conversation_id)
    draft = await db.get(Message, message_id)
    if draft is None or draft.conversation_id != conv.id or not draft.is_draft:
        raise NotFound("Draft not found")
    await db.delete(draft)
    await db.commit()


@router.post("/{conversation_id}/ai-reply", response_model=dict)
async def request_ai_reply(conversation_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> dict:
    """Ask the AI to (re)answer the latest customer message now, e.g. after handing a thread back."""
    conv = await _get(db, principal, conversation_id)
    latest = await db.scalar(
        select(Message)
        .where(Message.conversation_id == conv.id, Message.author_type == "contact")
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    if latest is None:
        raise AppError("No customer message to answer", code="nothing_to_answer")
    conv.ai_paused_until = utcnow()
    emit(db, principal.org_id, "message.received", {"conversation_id": conv.id, "message_id": latest.id})
    await db.commit()
    return {"queued": True}


@router.post("/{conversation_id}/suggest-reply", response_model=SuggestedReply)
async def suggest(conversation_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> SuggestedReply:
    """Draft the next reply with the AI for a teammate to edit. Nothing is sent and tool side effects
    (bookings, payment links) are rolled back."""
    conv = await _get(db, principal, conversation_id)
    gen, ctx = await suggest_reply(db, principal.org, conv)
    await db.rollback()
    if not gen.text:
        raise AppError(gen.error or "The AI could not draft a reply", code="no_suggestion")
    return SuggestedReply(body=gen.text, sources=ctx.sources)

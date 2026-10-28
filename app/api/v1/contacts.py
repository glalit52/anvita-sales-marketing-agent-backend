import uuid
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, CurrentPrincipal, ManagerPrincipal
from app.core.errors import AppError, Conflict, NotFound
from app.db.base import utcnow
from app.models import Appointment, ConsentRecord, Contact, ContactNote, Conversation, Message, Payment, Task
from app.schemas.common import Ok, Page
from app.schemas.crm import (
    BulkContactAction,
    ConsentIn,
    ConsentOut,
    ContactIn,
    ContactOut,
    ContactUpdate,
    NoteIn,
    NoteOut,
    TimelineEvent,
)
from app.schemas.inbox import ConversationOut
from app.services.audit import audit
from app.services.contacts import contact_event, set_consent
from app.services.events import emit
from app.services.phone import normalize_phone

router = APIRouter(prefix="/contacts", tags=["contacts"])

SORTS = {
    "recent": Contact.last_activity_at.desc().nulls_last(),
    "created": Contact.created_at.desc(),
    "name": Contact.name.asc(),
    "score": Contact.score.desc(),
    "value": Contact.value_minor.desc(),
}


async def _get(db: DB, principal: CurrentPrincipal, contact_id: uuid.UUID) -> Contact:
    c = await db.get(Contact, contact_id)
    if c is None or c.org_id != principal.org_id:
        raise NotFound("Contact not found")
    return c


def _dupe_error(e: IntegrityError) -> Conflict:
    field = "phone" if "phone" in str(e.orig) else "email"
    return Conflict(f"A contact with this {field} already exists", code=f"duplicate_{field}")


@router.get("", response_model=Page[ContactOut])
async def list_contacts(
    principal: CurrentPrincipal,
    db: DB,
    q: str | None = Query(None, description="Search name, phone, email or company"),
    stage: list[str] | None = Query(None),
    tag: list[str] | None = Query(None),
    source: str | None = None,
    channel: str | None = None,
    owner: str | None = Query(None, description="A user id, 'ai', or 'me'"),
    sort: Literal["recent", "created", "name", "score", "value"] = "recent",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ContactOut]:
    cond = [Contact.org_id == principal.org_id]
    if q:
        like = f"%{q.strip()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        ors = [Contact.name.ilike(like), Contact.email.ilike(like), Contact.company.ilike(like)]
        if len(digits) >= 4:
            ors.append(Contact.phone.contains(digits))
        cond.append(or_(*ors))
    if stage:
        cond.append(Contact.stage.in_(stage))
    if tag:
        cond.append(Contact.tags.overlap(tag))
    if source:
        cond.append(Contact.source == source)
    if channel:
        cond.append(Contact.channel == channel)
    if owner == "ai":
        cond.append(Contact.owner_id.is_(None))
    elif owner == "me":
        cond.append(Contact.owner_id == principal.user_id)
    elif owner:
        try:
            cond.append(Contact.owner_id == uuid.UUID(owner))
        except ValueError as e:
            raise AppError("owner must be a user id, 'ai' or 'me'", code="invalid_owner") from e
    total = await db.scalar(select(func.count()).select_from(Contact).where(*cond)) or 0
    rows = await db.scalars(select(Contact).where(*cond).order_by(SORTS[sort], Contact.id).limit(limit).offset(offset))
    return Page(items=[ContactOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.post("", response_model=ContactOut, status_code=201)
async def create_contact(body: ContactIn, principal: CurrentPrincipal, db: DB) -> ContactOut:
    data = body.model_dump(by_alias=False)
    data["phone"] = normalize_phone(body.phone, principal.org.country)
    data["tags"] = list(dict.fromkeys(t.strip().lower() for t in body.tags if t.strip()))
    contact = Contact(org_id=principal.org_id, utm={}, last_activity_at=utcnow(), **data)
    db.add(contact)
    try:
        await db.flush()
    except IntegrityError as e:
        raise _dupe_error(e) from e
    emit(db, principal.org_id, "contact.created", contact_event(contact))
    audit(
        db, principal.org_id, "contact.created", actor_id=principal.user_id, entity_type="contact", entity_id=contact.id
    )
    await db.commit()
    return ContactOut.model_validate(contact)


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(contact_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> ContactOut:
    return ContactOut.model_validate(await _get(db, principal, contact_id))


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(contact_id: uuid.UUID, body: ContactUpdate, principal: CurrentPrincipal, db: DB) -> ContactOut:
    contact = await _get(db, principal, contact_id)
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    if "phone" in changes:
        changes["phone"] = normalize_phone(changes["phone"], principal.org.country)
    if "tags" in changes and changes["tags"] is not None:
        changes["tags"] = list(dict.fromkeys(t.strip().lower() for t in changes["tags"] if t.strip()))
    old_stage = contact.stage
    for k, v in changes.items():
        setattr(contact, k, v)
    try:
        await db.flush()
    except IntegrityError as e:
        raise _dupe_error(e) from e
    emit(db, principal.org_id, "contact.updated", {**contact_event(contact), "changed": sorted(changes)})
    if "stage" in changes and changes["stage"] != old_stage:
        audit(
            db,
            principal.org_id,
            "contact.stage_changed",
            actor_id=principal.user_id,
            entity_type="contact",
            entity_id=contact.id,
            data={"from": old_stage, "to": changes["stage"]},
        )
    await db.commit()
    return ContactOut.model_validate(contact)


@router.delete("/{contact_id}", response_model=Ok)
async def delete_contact(contact_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    """Hard delete (also serves DPDP/GDPR erasure requests): cascades to conversations, notes and consents."""
    contact = await _get(db, principal, contact_id)
    audit(
        db,
        principal.org_id,
        "contact.deleted",
        actor_id=principal.user_id,
        entity_type="contact",
        entity_id=contact.id,
        data={"name": contact.name},
    )
    await db.delete(contact)
    await db.commit()
    return Ok()


@router.post("/bulk", response_model=Ok)
async def bulk_update(body: BulkContactAction, principal: CurrentPrincipal, db: DB) -> Ok:
    rows = (await db.scalars(select(Contact).where(Contact.org_id == principal.org_id, Contact.id.in_(body.ids)))).all()
    add = [t.strip().lower() for t in body.add_tags if t.strip()]
    remove = {t.strip().lower() for t in body.remove_tags}
    for c in rows:
        if add or remove:
            c.tags = [t for t in dict.fromkeys([*c.tags, *add]) if t not in remove]
        if body.stage:
            c.stage = body.stage
        if body.assign_to_ai:
            c.owner_id = None
        elif body.owner_id:
            c.owner_id = body.owner_id
    audit(db, principal.org_id, "contact.bulk_updated", actor_id=principal.user_id, data={"count": len(rows)})
    await db.commit()
    return Ok()


# ---- Consent ---------------------------------------------------------------------------------------


@router.get("/{contact_id}/consents", response_model=list[ConsentOut])
async def list_consents(contact_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> list[ConsentOut]:
    await _get(db, principal, contact_id)
    rows = await db.scalars(select(ConsentRecord).where(ConsentRecord.contact_id == contact_id))
    return [ConsentOut.model_validate(r) for r in rows]


@router.put("/{contact_id}/consents", response_model=ConsentOut)
async def put_consent(contact_id: uuid.UUID, body: ConsentIn, principal: CurrentPrincipal, db: DB) -> ConsentOut:
    await _get(db, principal, contact_id)
    rec = await set_consent(
        db,
        principal.org_id,
        contact_id,
        channel=body.channel,
        status=body.status,
        source=body.source,
        purpose=body.purpose,
    )
    audit(
        db,
        principal.org_id,
        "consent.updated",
        actor_id=principal.user_id,
        entity_type="contact",
        entity_id=contact_id,
        data=body.model_dump(),
    )
    await db.commit()
    return ConsentOut.model_validate(rec)


# ---- Notes -----------------------------------------------------------------------------------------


@router.get("/{contact_id}/notes", response_model=list[NoteOut])
async def list_notes(contact_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> list[NoteOut]:
    await _get(db, principal, contact_id)
    rows = await db.scalars(
        select(ContactNote)
        .where(ContactNote.contact_id == contact_id)
        .order_by(ContactNote.pinned.desc(), ContactNote.created_at.desc())
    )
    return [NoteOut.model_validate(r) for r in rows]


@router.post("/{contact_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(contact_id: uuid.UUID, body: NoteIn, principal: CurrentPrincipal, db: DB) -> NoteOut:
    await _get(db, principal, contact_id)
    note = ContactNote(
        org_id=principal.org_id, contact_id=contact_id, author_id=principal.user_id, body=body.body, pinned=body.pinned
    )
    db.add(note)
    await db.commit()
    return NoteOut.model_validate(note)


@router.delete("/{contact_id}/notes/{note_id}", response_model=Ok)
async def delete_note(contact_id: uuid.UUID, note_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    note = await db.get(ContactNote, note_id)
    if note is None or note.contact_id != contact_id:
        raise NotFound("Note not found")
    await db.delete(note)
    await db.commit()
    return Ok()


# ---- Activity --------------------------------------------------------------------------------------


@router.get("/{contact_id}/conversations", response_model=list[ConversationOut])
async def contact_conversations(contact_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> list[ConversationOut]:
    await _get(db, principal, contact_id)
    rows = await db.scalars(
        select(Conversation)
        .where(Conversation.contact_id == contact_id)
        .order_by(Conversation.last_message_at.desc().nulls_last())
    )
    return [ConversationOut.model_validate(r) for r in rows]


@router.get("/{contact_id}/timeline", response_model=list[TimelineEvent])
async def timeline(
    contact_id: uuid.UUID, principal: CurrentPrincipal, db: DB, limit: int = Query(100, le=500)
) -> list[TimelineEvent]:
    """Everything that happened with this contact, newest first, across modules."""
    await _get(db, principal, contact_id)
    events: list[TimelineEvent] = []
    msgs = await db.execute(
        select(Message, Conversation.channel_type)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.contact_id == contact_id, Message.is_draft.is_(False))
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    for m, channel in msgs:
        kind = "ai" if m.author_type == "ai" else "note" if m.is_note else "message"
        title = {"contact": "Message received", "ai": "AI replied", "human": "Team replied", "system": "System"}[
            m.author_type
        ]
        events.append(
            TimelineEvent(
                id=f"msg:{m.id}",
                kind=kind,
                title=title,
                detail=m.body[:300],
                at=m.created_at,
                channel=channel,
                actor=m.author_name,
                meta=f"Cited {', '.join(m.sources)}" if m.sources else None,
            )
        )
    for a in await db.scalars(select(Appointment).where(Appointment.contact_id == contact_id).limit(limit)):
        events.append(
            TimelineEvent(
                id=f"appt:{a.id}",
                kind="booking",
                title=f"Appointment {a.status}",
                at=a.created_at,
                detail=a.starts_at.isoformat(),
                actor=a.source,
            )
        )
    for p in await db.scalars(select(Payment).where(Payment.contact_id == contact_id).limit(limit)):
        events.append(
            TimelineEvent(
                id=f"pay:{p.id}",
                kind="payment",
                title=f"Payment {p.status}",
                detail=f"{p.currency} {p.amount_minor / 100:,.2f} · {p.purpose}",
                at=p.paid_at or p.created_at,
            )
        )
    for n in await db.scalars(select(ContactNote).where(ContactNote.contact_id == contact_id).limit(limit)):
        events.append(
            TimelineEvent(id=f"note:{n.id}", kind="note", title="Note added", detail=n.body[:300], at=n.created_at)
        )
    for t in await db.scalars(select(Task).where(Task.contact_id == contact_id).limit(limit)):
        events.append(
            TimelineEvent(
                id=f"task:{t.id}",
                kind="task",
                title=t.title,
                at=t.created_at,
                actor="AI" if t.created_by_type == "ai" else None,
                meta="Done" if t.done_at else None,
            )
        )
    events.sort(key=lambda e: e.at, reverse=True)
    return events[:limit]

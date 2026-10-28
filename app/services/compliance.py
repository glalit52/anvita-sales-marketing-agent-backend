"""Data-subject requests: export everything about a person, or erase it."""

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import (
    Appointment,
    ConsentRecord,
    Contact,
    ContactNote,
    Conversation,
    DataRequest,
    Deal,
    Message,
    Payment,
    Task,
)
from app.services.audit import audit
from app.services.phone import normalize_phone


async def find_subject(db: AsyncSession, org_id, subject: str, country: str = "IN") -> Contact | None:  # noqa: ANN001
    phone = normalize_phone(subject, country) if any(ch.isdigit() for ch in subject) else None
    cond = [func.lower(Contact.email) == subject.lower().strip(), Contact.name.ilike(subject.strip())]
    if phone:
        cond.append(Contact.phone == phone)
    return await db.scalar(select(Contact).where(Contact.org_id == org_id, or_(*cond)).limit(1))


def _row(obj: Any) -> dict[str, Any]:
    raw = {c.key: getattr(obj, c.key) for c in obj.__table__.columns if c.key not in ("org_id", "tsv")}
    return json.loads(json.dumps(raw, default=str))  # JSON-safe (dates, UUIDs, decimals) for storage and download


async def export_contact(db: AsyncSession, contact: Contact) -> dict[str, Any]:
    convs = (await db.scalars(select(Conversation).where(Conversation.contact_id == contact.id))).all()
    conv_ids = [c.id for c in convs]
    msgs = (await db.scalars(select(Message).where(Message.conversation_id.in_(conv_ids)))).all() if conv_ids else []
    out: dict[str, Any] = {
        "contact": _row(contact),
        "conversations": [_row(c) for c in convs],
        "messages": [_row(m) for m in msgs],
    }
    for key, model in (
        ("consents", ConsentRecord),
        ("notes", ContactNote),
        ("deals", Deal),
        ("appointments", Appointment),
        ("payments", Payment),
        ("tasks", Task),
    ):
        out[key] = [_row(r) for r in (await db.scalars(select(model).where(model.contact_id == contact.id))).all()]
    return out


async def process(db: AsyncSession, req: DataRequest, actor_id) -> DataRequest:  # noqa: ANN001
    contact = await db.get(Contact, req.contact_id) if req.contact_id else None
    if contact is None:
        req.status, req.note = "rejected", "No matching contact found"
    elif req.type == "export":
        req.result = await export_contact(db, contact)
        req.status = "completed"
    else:
        # Payments are kept for tax law (contact link is cleared by ON DELETE SET NULL).
        audit(
            db,
            req.org_id,
            "data_request.erased",
            actor_id=actor_id,
            entity_type="contact",
            entity_id=contact.id,
            data={"request": req.number},
        )
        await db.delete(contact)
        req.contact_id = None
        req.result = {"erased": True}
        req.status = "completed"
    req.completed_at = utcnow()
    await db.flush()
    return req

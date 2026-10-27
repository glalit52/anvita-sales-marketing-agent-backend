import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import ConsentRecord, Contact
from app.services.phone import normalize_phone


async def find_contact(
    db: AsyncSession, org_id: uuid.UUID, *, phone: str | None = None, email: str | None = None
) -> Contact | None:
    if phone:
        c = await db.scalar(select(Contact).where(Contact.org_id == org_id, Contact.phone == phone))
        if c:
            return c
    if email:
        return await db.scalar(
            select(Contact).where(Contact.org_id == org_id, func.lower(Contact.email) == email.lower())
        )
    return None


async def get_or_create_contact(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str | None,
    phone: str | None = None,
    email: str | None = None,
    channel: str | None = None,
    source: str | None = None,
    country: str = "IN",
) -> tuple[Contact, bool]:
    phone = normalize_phone(phone, country)
    existing = await find_contact(db, org_id, phone=phone, email=email)
    if existing:
        return existing, False
    contact = Contact(
        org_id=org_id,
        name=name or phone or email or "Website visitor",
        phone=phone,
        email=email,
        channel=channel,
        source=source,
        stage="new",
        tags=[],
        utm={},
        attributes={},
        last_activity_at=utcnow(),
    )
    db.add(contact)
    await db.flush()
    return contact, True


async def set_consent(
    db: AsyncSession,
    org_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    channel: str,
    status: str,
    source: str,
    purpose: str = "marketing",
) -> ConsentRecord:
    rec = await db.scalar(
        select(ConsentRecord).where(
            ConsentRecord.contact_id == contact_id,
            ConsentRecord.channel == channel,
            ConsentRecord.purpose == purpose,
        )
    )
    now = utcnow()
    if rec is None:
        rec = ConsentRecord(org_id=org_id, contact_id=contact_id, channel=channel, purpose=purpose)
        db.add(rec)
    rec.status = status
    rec.source = source
    rec.captured_at = now
    return rec

"""Segment rules → SQL over contacts.

rules = {"match": "all" | "any", "conditions": [{"field", "op", "value"}]}

fields: stage, tag, source, channel, city, language, owner (user id | "ai"), score, value (major units),
        last_activity_days, created_days, last_visit_days (completed appointment), consent_whatsapp,
        consent_sms, consent_email (marketing opt-in: true/false)
ops:    eq, neq, in, nin, gt, gte, lt, lte, contains (tags), exists
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.base import utcnow
from app.models import Appointment, ConsentRecord, Contact

FIELDS = {
    "stage",
    "tag",
    "source",
    "channel",
    "city",
    "language",
    "owner",
    "score",
    "value",
    "last_activity_days",
    "created_days",
    "last_visit_days",
    "consent_whatsapp",
    "consent_sms",
    "consent_email",
}


def _cmp(col: Any, op: str, value: Any) -> ColumnElement[bool]:
    match op:
        case "eq":
            return col == value
        case "neq":
            return col != value
        case "in":
            return col.in_(value if isinstance(value, list) else [value])
        case "nin":
            return col.notin_(value if isinstance(value, list) else [value])
        case "gt":
            return col > value
        case "gte":
            return col >= value
        case "lt":
            return col < value
        case "lte":
            return col <= value
        case "exists":
            return col.is_not(None) if value else col.is_(None)
    raise AppError(f"Unsupported operator {op}", code="invalid_segment")


def _days_ago(col: Any, op: str, days: int) -> ColumnElement[bool]:
    # "last_activity_days gt 30" = last activity more than 30 days ago (i.e. before the cutoff).
    cutoff = utcnow() - timedelta(days=int(days))
    flipped = {"gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte"}.get(op, op)
    return _cmp(col, flipped, cutoff)


def condition(c: dict[str, Any]) -> ColumnElement[bool]:
    field, op, value = c.get("field"), c.get("op", "eq"), c.get("value")
    if field not in FIELDS:
        raise AppError(f"Unknown segment field {field}", code="invalid_segment")
    simple = {
        "stage": Contact.stage,
        "source": Contact.source,
        "channel": Contact.channel,
        "city": Contact.city,
        "language": Contact.language,
        "score": Contact.score,
    }
    if field in simple:
        return _cmp(simple[field], op, value)
    if field == "value":
        return _cmp(Contact.value_minor, op, int(float(value) * 100))
    if field == "tag":
        tags = value if isinstance(value, list) else [value]
        expr = Contact.tags.overlap([str(t).lower() for t in tags])
        return ~expr if op in ("neq", "nin") else expr
    if field == "owner":
        if value == "ai":
            return Contact.owner_id.is_(None) if op != "neq" else Contact.owner_id.is_not(None)
        return _cmp(Contact.owner_id, op, uuid.UUID(str(value)))
    if field == "last_activity_days":
        return _days_ago(Contact.last_activity_at, op, value)
    if field == "created_days":
        return _days_ago(Contact.created_at, op, value)
    if field == "last_visit_days":
        last_visit = (
            select(func.max(Appointment.starts_at))
            .where(Appointment.contact_id == Contact.id, Appointment.status == "completed")
            .scalar_subquery()
        )
        return _days_ago(last_visit, op, value)
    channel = field.removeprefix("consent_")
    opted = exists().where(
        ConsentRecord.contact_id == Contact.id,
        ConsentRecord.channel == channel,
        ConsentRecord.purpose == "marketing",
        ConsentRecord.status == "opted_in",
    )
    want = value in (True, "true", "yes", 1)
    return opted if (want and op != "neq") or (not want and op == "neq") else ~opted


def where(org_id: uuid.UUID, rules: dict[str, Any] | None) -> ColumnElement[bool]:
    conds = [condition(c) for c in (rules or {}).get("conditions", [])]
    if not conds:
        combined: ColumnElement[bool] = true()
    elif (rules or {}).get("match", "all") == "any":
        combined = or_(*conds) if conds else false()
    else:
        combined = and_(*conds)
    return and_(Contact.org_id == org_id, combined)


async def count(db: AsyncSession, org_id: uuid.UUID, rules: dict[str, Any] | None) -> int:
    return int(await db.scalar(select(func.count()).select_from(Contact).where(where(org_id, rules))) or 0)


async def contacts(db: AsyncSession, org_id: uuid.UUID, rules: dict[str, Any] | None, limit: int | None = None):
    q = select(Contact).where(where(org_id, rules)).order_by(Contact.created_at)
    if limit:
        q = q.limit(limit)
    return list((await db.scalars(q)).all())


def has_consent(channel: str) -> ColumnElement[bool]:
    return exists().where(
        ConsentRecord.contact_id == Contact.id,
        ConsentRecord.channel == channel,
        ConsentRecord.purpose == "marketing",
        ConsentRecord.status == "opted_in",
    )

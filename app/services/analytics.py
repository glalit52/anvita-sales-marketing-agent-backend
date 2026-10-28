"""Reporting over the operational tables: overview KPIs, revenue by source, attribution, AI performance,
team performance, prospecting and sequence funnels. All amounts are in minor units."""

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, case, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import (
    AgentRun,
    Appointment,
    Contact,
    Conversation,
    Enrollment,
    Membership,
    Message,
    Payment,
    ProspectRow,
    SequenceSend,
    User,
)
from app.services import providers
from app.services.integrations import try_resolve_for_org

RANGES = {"7d": 7, "30d": 30, "90d": 90}


def window(range_: str) -> tuple[datetime, datetime, datetime]:
    days = RANGES.get(range_, 30)
    now = utcnow()
    return now - timedelta(days=days), now, now - timedelta(days=2 * days)


async def _scalar(db: AsyncSession, q: Any) -> int:
    return int(await db.scalar(q) or 0)


async def _kpis(db: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime) -> dict[str, Any]:
    leads = await _scalar(
        db,
        select(func.count())
        .select_from(Contact)
        .where(Contact.org_id == org_id, Contact.created_at >= start, Contact.created_at < end),
    )
    bookings = await _scalar(
        db,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.org_id == org_id,
            Appointment.created_at >= start,
            Appointment.created_at < end,
            Appointment.status != "cancelled",
        ),
    )
    revenue = await _scalar(
        db,
        select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
            Payment.org_id == org_id, Payment.status == "paid", Payment.paid_at >= start, Payment.paid_at < end
        ),
    )
    runs = (
        await db.execute(
            select(
                func.count(func.distinct(AgentRun.conversation_id)),
                func.count(func.distinct(AgentRun.conversation_id)).filter(AgentRun.outcome == "escalated"),
            ).where(AgentRun.org_id == org_id, AgentRun.created_at >= start, AgentRun.created_at < end)
        )
    ).one()
    ai_res = round(100 * (runs[0] - runs[1]) / runs[0]) if runs[0] else 0
    frt = await first_response_seconds(db, org_id, start, end)
    return {"leads": leads, "bookings": bookings, "revenueMinor": revenue, "aiResolution": ai_res, "frtSeconds": frt}


async def first_response_seconds(db: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime) -> int:
    """Median seconds from a conversation's first inbound message to the first reply."""
    first_in = (
        select(Message.conversation_id, func.min(Message.created_at).label("t_in"))
        .where(
            Message.org_id == org_id,
            Message.direction == "inbound",
            Message.created_at >= start,
            Message.created_at < end,
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    first_out = (
        select(
            Message.conversation_id,
            func.min(Message.created_at).label("t_out"),
        )
        .join(first_in, first_in.c.conversation_id == Message.conversation_id)
        .where(
            Message.direction == "outbound",
            Message.is_note.is_(False),
            Message.created_at >= first_in.c.t_in,
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    val = await db.scalar(
        select(
            func.percentile_cont(0.5).within_group(func.extract("epoch", first_out.c.t_out - first_in.c.t_in))
        ).join_from(first_in, first_out, first_in.c.conversation_id == first_out.c.conversation_id)
    )
    return int(val or 0)


async def ad_spend(db: AsyncSession, org_id: uuid.UUID, days: int) -> int | None:
    ads = await try_resolve_for_org(db, org_id, "ads")
    if ads is None:
        return None
    try:
        return sum(c.spend_minor for c in await ads.campaigns(str(org_id), days=days))
    except providers.ProviderNotImplemented:
        return None


async def overview(db: AsyncSession, org_id: uuid.UUID, range_: str) -> dict[str, Any]:
    start, end, prev = window(range_)
    cur = await _kpis(db, org_id, start, end)
    before = await _kpis(db, org_id, prev, start)

    def delta(k: str) -> float | None:
        return round(100 * (cur[k] - before[k]) / before[k], 1) if before[k] else None

    spend = await ad_spend(db, org_id, RANGES.get(range_, 30))
    return {
        **cur,
        "adSpendMinor": spend,
        "roas": round(cur["revenueMinor"] / spend, 2) if spend else None,
        "deltas": {k: delta(k) for k in ("leads", "bookings", "revenueMinor", "aiResolution")},
    }


async def revenue_by_source(db: AsyncSession, org_id: uuid.UUID, range_: str) -> list[dict[str, Any]]:
    start, end, _ = window(range_)
    src = func.coalesce(Contact.source, "direct")
    leads = dict(
        (
            await db.execute(
                select(src, func.count())
                .where(Contact.org_id == org_id, Contact.created_at >= start, Contact.created_at < end)
                .group_by(src)
            )
        ).all()
    )
    rev = (
        await db.execute(
            select(src, func.coalesce(func.sum(Payment.amount_minor), 0), func.count(func.distinct(Payment.contact_id)))
            .select_from(Payment)
            .join(Contact, Contact.id == Payment.contact_id)
            .where(Payment.org_id == org_id, Payment.status == "paid", Payment.paid_at >= start, Payment.paid_at < end)
            .group_by(src)
        )
    ).all()
    bookings = dict(
        (
            await db.execute(
                select(src, func.count())
                .select_from(Appointment)
                .join(Contact, Contact.id == Appointment.contact_id)
                .where(
                    Appointment.org_id == org_id,
                    Appointment.created_at >= start,
                    Appointment.created_at < end,
                    Appointment.status != "cancelled",
                )
                .group_by(src)
            )
        ).all()
    )
    revmap = {s: (int(r), int(c)) for s, r, c in rev}
    sources = set(leads) | set(revmap) | set(bookings)
    out = [
        {
            "source": s,
            "leads": int(leads.get(s, 0)),
            "bookings": int(bookings.get(s, 0)),
            "customers": revmap.get(s, (0, 0))[1],
            "revenueMinor": revmap.get(s, (0, 0))[0],
        }
        for s in sources
    ]
    return sorted(out, key=lambda r: (-r["revenueMinor"], -r["leads"]))


async def weekly_revenue(db: AsyncSession, org_id: uuid.UUID, weeks: int = 12) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(weeks=weeks)
    wk = func.date_trunc("week", Payment.paid_at)
    rows = (
        await db.execute(
            select(wk, func.sum(Payment.amount_minor), func.count())
            .where(Payment.org_id == org_id, Payment.status == "paid", Payment.paid_at >= since)
            .group_by(wk)
            .order_by(wk)
        )
    ).all()
    awk = func.date_trunc("week", Appointment.starts_at)
    appts = dict(
        (
            await db.execute(
                select(awk, func.count())
                .where(Appointment.org_id == org_id, Appointment.starts_at >= since, Appointment.status != "cancelled")
                .group_by(awk)
            )
        ).all()
    )
    by_week = {w: (int(s), int(c)) for w, s, c in rows}
    out = []
    start = (utcnow() - timedelta(weeks=weeks - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=start.weekday())
    for i in range(weeks):
        w = start + timedelta(weeks=i)
        rev, pays = next(((v[0], v[1]) for k, v in by_week.items() if k.date() == w.date()), (0, 0))
        books = next((int(v) for k, v in appts.items() if k.date() == w.date()), 0)
        out.append({"week": w.date().isoformat(), "revenueMinor": rev, "payments": pays, "bookings": books})
    return out


async def attribution(db: AsyncSession, org_id: uuid.UUID, model: str, range_: str) -> list[dict[str, Any]]:
    """Credit paid revenue to touchpoints: the contact's acquisition source plus every channel they talked to us
    on before paying. first = all credit to the first touch, last = to the last, linear = split evenly."""
    start, end, _ = window(range_)
    pays = (
        await db.execute(
            select(Payment.contact_id, Payment.amount_minor, Payment.paid_at, Contact.source, Contact.utm)
            .select_from(Payment)
            .join(Contact, Contact.id == Payment.contact_id)
            .where(Payment.org_id == org_id, Payment.status == "paid", Payment.paid_at >= start, Payment.paid_at < end)
        )
    ).all()
    contact_ids = list({p.contact_id for p in pays})
    convs: dict[uuid.UUID, list[tuple[datetime, str]]] = defaultdict(list)
    if contact_ids:
        for cid, at, ch in (
            await db.execute(
                select(Conversation.contact_id, Conversation.created_at, Conversation.channel_type).where(
                    Conversation.contact_id.in_(contact_ids)
                )
            )
        ).all():
            convs[cid].append((at, ch))
    credit: dict[str, dict[str, float]] = defaultdict(lambda: {"revenueMinor": 0.0, "conversions": 0.0})
    for p in pays:
        first = (p.utm or {}).get("source") or p.source or "direct"
        touches = [first] + [ch for at, ch in sorted(convs.get(p.contact_id, [])) if at <= p.paid_at]
        if model == "first":
            weights = {touches[0]: 1.0}
        elif model == "last":
            weights = {touches[-1]: 1.0}
        else:
            weights = defaultdict(float)
            for t in touches:
                weights[t] += 1 / len(touches)
        for t, w in weights.items():
            credit[t]["revenueMinor"] += p.amount_minor * w
            credit[t]["conversions"] += w
    total = sum(v["revenueMinor"] for v in credit.values()) or 1
    return sorted(
        (
            {
                "touchpoint": k,
                "revenueMinor": round(v["revenueMinor"]),
                "conversions": round(v["conversions"], 2),
                "share": round(100 * v["revenueMinor"] / total, 1),
            }
            for k, v in credit.items()
        ),
        key=lambda r: -r["revenueMinor"],
    )


async def funnel(db: AsyncSession, org_id: uuid.UUID, range_: str) -> list[dict[str, Any]]:
    start, end, _ = window(range_)
    new = select(Contact.id).where(Contact.org_id == org_id, Contact.created_at >= start, Contact.created_at < end)
    leads = await _scalar(db, select(func.count()).select_from(new.subquery()))
    engaged = await _scalar(
        db,
        select(func.count(func.distinct(Conversation.contact_id))).where(
            Conversation.contact_id.in_(new), Conversation.last_inbound_at.is_not(None)
        ),
    )
    booked = await _scalar(
        db,
        select(func.count(func.distinct(Appointment.contact_id))).where(
            Appointment.contact_id.in_(new), Appointment.status != "cancelled"
        ),
    )
    paid = await _scalar(
        db,
        select(func.count(func.distinct(Payment.contact_id))).where(
            Payment.contact_id.in_(new), Payment.status == "paid"
        ),
    )
    return [
        {"stage": "Leads", "count": leads},
        {"stage": "Engaged", "count": engaged},
        {"stage": "Booked", "count": booked},
        {"stage": "Paid", "count": paid},
    ]


async def ai_performance(db: AsyncSession, org_id: uuid.UUID, weeks: int = 8) -> dict[str, Any]:
    since = utcnow() - timedelta(weeks=weeks)
    wk = func.date_trunc("week", AgentRun.created_at)
    trend = (
        await db.execute(
            select(
                wk,
                func.count(func.distinct(AgentRun.conversation_id)),
                func.count(func.distinct(AgentRun.conversation_id)).filter(AgentRun.outcome == "escalated"),
            )
            .where(AgentRun.org_id == org_id, AgentRun.created_at >= since, AgentRun.kind == "reply")
            .group_by(wk)
            .order_by(wk)
        )
    ).all()
    reasons = (
        await db.execute(
            select(AgentRun.reason, func.count())
            .where(AgentRun.org_id == org_id, AgentRun.created_at >= since, AgentRun.outcome == "escalated")
            .group_by(AgentRun.reason)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()
    by_source = (
        await db.execute(
            select(
                case((Appointment.source == "ai", "ai"), else_="human"),
                func.count(),
            )
            .where(Appointment.org_id == org_id, Appointment.created_at >= since, Appointment.status != "cancelled")
            .group_by(text("1"))
        )
    ).all()
    cost = await db.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
            AgentRun.org_id == org_id, AgentRun.created_at >= since
        )
    )
    return {
        "resolutionTrend": [
            {"week": w.date().isoformat(), "conversations": c, "resolution": round(100 * (c - e) / c) if c else 0}
            for w, c, e in trend
        ],
        "escalationReasons": [{"reason": r or "Other", "count": n} for r, n in reasons],
        "bookings": {k: int(v) for k, v in by_source},
        "costUsd": float(cost or 0),
        "gaps": await knowledge_gaps(db, org_id, limit=8),
    }


async def knowledge_gaps(db: AsyncSession, org_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
    """Customer questions the AI handed over because it lacked an answer, grouped by wording."""
    rows = (
        await db.execute(
            select(Message.body, Message.created_at)
            .join(AgentRun, AgentRun.trigger_message_id == Message.id)
            .where(
                AgentRun.org_id == org_id,
                AgentRun.outcome == "escalated",
                AgentRun.created_at >= utcnow() - timedelta(days=60),
            )
            .order_by(Message.created_at.desc())
            .limit(500)
        )
    ).all()
    groups: dict[str, dict[str, Any]] = {}
    for body, at in rows:
        key = " ".join(body.lower().split()[:8])
        g = groups.setdefault(key, {"question": body[:300], "count": 0, "lastAsked": at})
        g["count"] += 1
    return sorted(groups.values(), key=lambda g: -g["count"])[:limit]


async def team(db: AsyncSession, org_id: uuid.UUID, range_: str) -> list[dict[str, Any]]:
    start, end, _ = window(range_)
    members = (
        await db.execute(
            select(User.id, User.name, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == org_id)
        )
    ).all()
    replies = dict(
        (
            await db.execute(
                select(Message.author_id, func.count())
                .where(
                    Message.org_id == org_id,
                    Message.author_type == "human",
                    Message.direction == "outbound",
                    Message.created_at >= start,
                    Message.created_at < end,
                )
                .group_by(Message.author_id)
            )
        ).all()
    )
    convs = dict(
        (
            await db.execute(
                select(Conversation.assignee_id, func.count())
                .where(Conversation.org_id == org_id, Conversation.last_message_at >= start)
                .group_by(Conversation.assignee_id)
            )
        ).all()
    )
    revenue = dict(
        (
            await db.execute(
                select(Contact.owner_id, func.coalesce(func.sum(Payment.amount_minor), 0))
                .select_from(Payment)
                .join(Contact, Contact.id == Payment.contact_id)
                .where(Payment.org_id == org_id, Payment.status == "paid", Payment.paid_at >= start)
                .group_by(Contact.owner_id)
            )
        ).all()
    )
    bookings = dict(
        (
            await db.execute(
                select(Contact.owner_id, func.count())
                .select_from(Appointment)
                .join(Contact, Contact.id == Appointment.contact_id)
                .where(Appointment.org_id == org_id, Appointment.created_at >= start, Appointment.status != "cancelled")
                .group_by(Contact.owner_id)
            )
        ).all()
    )
    return [
        {
            "userId": str(uid),
            "name": name,
            "role": role,
            "conversations": int(convs.get(uid, 0)),
            "replies": int(replies.get(uid, 0)),
            "bookings": int(bookings.get(uid, 0)),
            "revenueMinor": int(revenue.get(uid, 0)),
        }
        for uid, name, role in members
    ]


async def outbound(db: AsyncSession, org_id: uuid.UUID, range_: str) -> dict[str, Any]:
    start, end, _ = window(range_)
    rows = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(ProspectRow.enriched.is_(True)),
                func.count().filter(ProspectRow.contact_id.is_not(None)),
            ).where(ProspectRow.org_id == org_id, ProspectRow.created_at >= start)
        )
    ).one()
    enr = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Enrollment.replied_at.is_not(None)),
                func.count().filter(Enrollment.reply_class == "Interested"),
            ).where(Enrollment.org_id == org_id, Enrollment.created_at >= start)
        )
    ).one()
    sent = await _scalar(
        db,
        select(func.count())
        .select_from(SequenceSend)
        .where(SequenceSend.org_id == org_id, SequenceSend.sent_at >= start, SequenceSend.status == "sent"),
    )
    enrolled_contacts = select(Enrollment.contact_id).where(Enrollment.org_id == org_id, Enrollment.created_at >= start)
    booked = await _scalar(
        db,
        select(func.count(func.distinct(Appointment.contact_id))).where(
            Appointment.contact_id.in_(enrolled_contacts), Appointment.created_at >= start
        ),
    )
    return {
        "funnel": [
            {"stage": "Prospects found", "count": rows[0]},
            {"stage": "Enriched", "count": rows[1]},
            {"stage": "Added to CRM", "count": rows[2]},
            {"stage": "Enrolled", "count": enr[0]},
            {"stage": "Replied", "count": enr[1]},
            {"stage": "Interested", "count": enr[2]},
            {"stage": "Booked", "count": booked},
        ],
        "messagesSent": sent,
        "replyRate": round(100 * enr[1] / enr[0], 1) if enr[0] else 0,
    }


def as_int(col: Any) -> Any:
    return cast(col, Integer)

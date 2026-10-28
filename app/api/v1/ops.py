"""Channels, notifications and the home dashboard."""

import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from sqlalchemy import Date, cast, func, or_, select, text, update

from app.api.deps import DB, AdminPrincipal, CurrentPrincipal
from app.core.errors import NotFound
from app.db.base import utcnow
from app.models import AgentRun, Appointment, Channel, Contact, Conversation, Notification, Payment, Task
from app.schemas.common import Ok
from app.schemas.inbox import ChannelIn, ChannelOut, ChannelUpdate
from app.schemas.ops import DailyPoint, DashboardOut, MarkReadIn, NotificationOut
from app.services.audit import audit

router = APIRouter(tags=["operate"])


# ---- Channels --------------------------------------------------------------------------------------


@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(principal: CurrentPrincipal, db: DB) -> list[ChannelOut]:
    rows = await db.scalars(select(Channel).where(Channel.org_id == principal.org_id).order_by(Channel.type))
    return [ChannelOut.model_validate(r) for r in rows]


@router.post("/channels", response_model=ChannelOut, status_code=201)
async def create_channel(body: ChannelIn, principal: AdminPrincipal, db: DB) -> ChannelOut:
    ch = Channel(org_id=principal.org_id, status="connected", **body.model_dump(by_alias=False))
    db.add(ch)
    audit(db, principal.org_id, "channel.connected", actor_id=principal.user_id, data={"type": body.type})
    await db.commit()
    return ChannelOut.model_validate(ch)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(channel_id: uuid.UUID, body: ChannelUpdate, principal: AdminPrincipal, db: DB) -> ChannelOut:
    ch = await db.get(Channel, channel_id)
    if ch is None:
        raise NotFound("Channel not found")
    for k, v in body.model_dump(exclude_unset=True, by_alias=False).items():
        setattr(ch, k, v)
    await db.commit()
    return ChannelOut.model_validate(ch)


@router.delete("/channels/{channel_id}", response_model=Ok)
async def delete_channel(channel_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> Ok:
    ch = await db.get(Channel, channel_id)
    if ch is None:
        raise NotFound("Channel not found")
    await db.delete(ch)
    audit(db, principal.org_id, "channel.removed", actor_id=principal.user_id, data={"type": ch.type})
    await db.commit()
    return Ok()


# ---- Notifications ---------------------------------------------------------------------------------


def _mine(principal: CurrentPrincipal):  # noqa: ANN202
    return [
        Notification.org_id == principal.org_id,
        or_(Notification.user_id.is_(None), Notification.user_id == principal.user_id),
    ]


@router.get("/notifications", response_model=list[NotificationOut])
async def list_notifications(
    principal: CurrentPrincipal, db: DB, tab: str | None = None, unread: bool = False, limit: int = Query(50, le=200)
) -> list[NotificationOut]:
    q = select(Notification).where(*_mine(principal))
    if tab:
        q = q.where(Notification.tab == tab)
    if unread:
        q = q.where(Notification.read_at.is_(None))
    rows = await db.scalars(q.order_by(Notification.created_at.desc()).limit(limit))
    return [
        NotificationOut.model_validate(
            {**{c.key: getattr(n, c.key) for c in Notification.__table__.columns}, "unread": n.read_at is None}
        )
        for n in rows
    ]


@router.get("/notifications/unread-count", response_model=dict)
async def unread_count(principal: CurrentPrincipal, db: DB) -> dict:
    n = await db.scalar(
        select(func.count()).select_from(Notification).where(*_mine(principal), Notification.read_at.is_(None))
    )
    return {"count": n or 0}


@router.post("/notifications/read", response_model=Ok)
async def mark_read(principal: CurrentPrincipal, db: DB, body: MarkReadIn | None = None) -> Ok:
    """Mark the given notifications (or all, when no ids are sent) as read."""
    q = update(Notification).where(*_mine(principal), Notification.read_at.is_(None))
    if body and body.ids:
        q = q.where(Notification.id.in_(body.ids))
    await db.execute(q.values(read_at=utcnow()))
    await db.commit()
    return Ok()


# ---- Dashboard -------------------------------------------------------------------------------------


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(principal: CurrentPrincipal, db: DB, days: int = Query(30, ge=1, le=365)) -> DashboardOut:
    org_id = principal.org_id
    tz = ZoneInfo(principal.org.timezone)
    now = utcnow()
    first_day = now.astimezone(tz).date() - timedelta(days=days - 1)
    since = datetime.combine(first_day, time.min, tzinfo=tz)

    async def daily(col, agg, *where) -> dict:  # noqa: ANN001 - count/sum per org-local day
        bucket = cast(func.timezone(principal.org.timezone, col), Date)
        rows = await db.execute(select(bucket, agg).where(*where, col >= since).group_by(text("1")))
        return dict(rows.all())

    leads = await daily(Contact.created_at, func.count(), Contact.org_id == org_id)
    ai = await daily(
        AgentRun.created_at,
        func.count(func.distinct(AgentRun.conversation_id)),
        AgentRun.org_id == org_id,
        AgentRun.outcome.in_(["replied", "drafted"]),
    )
    booked = await daily(Appointment.created_at, func.count(), Appointment.org_id == org_id)
    revenue = await daily(
        Payment.paid_at, func.sum(Payment.amount_minor), Payment.org_id == org_id, Payment.status == "paid"
    )
    trend = [
        DailyPoint(
            date=d,
            leads=leads.get(d, 0),
            ai_handled=ai.get(d, 0),
            booked=booked.get(d, 0),
            revenue_minor=int(revenue.get(d, 0) or 0),
        )
        for d in (first_day + timedelta(days=i) for i in range(days))
    ]

    conversations = (
        await db.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.org_id == org_id, Conversation.last_message_at >= since)
        )
        or 0
    )
    escalated = (
        await db.scalar(
            select(func.count(func.distinct(AgentRun.conversation_id))).where(
                AgentRun.org_id == org_id, AgentRun.created_at >= since, AgentRun.outcome == "escalated"
            )
        )
        or 0
    )
    ai_convs = (
        await db.scalar(
            select(func.count(func.distinct(AgentRun.conversation_id))).where(
                AgentRun.org_id == org_id, AgentRun.created_at >= since, AgentRun.outcome != "skipped"
            )
        )
        or 0
    )
    tasks = (
        await db.execute(
            select(
                func.count().filter(Task.done_at.is_(None)),
                func.count().filter(Task.done_at.is_(None) & (Task.due_at < now)),
            ).where(Task.org_id == org_id)
        )
    ).one()
    escalations_open = (
        await db.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.org_id == org_id,
                Conversation.escalation_reason.is_not(None),
                Conversation.status != "closed",
            )
        )
        or 0
    )

    # First response time: first non-contact reply after each conversation's first inbound message.
    frt = await db.scalar(
        text("""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM r.first_reply - f.first_in))
        FROM (SELECT conversation_id, min(created_at) AS first_in FROM messages
              WHERE org_id = :org AND author_type = 'contact' AND created_at >= :since GROUP BY 1) f
        JOIN LATERAL (SELECT min(created_at) AS first_reply FROM messages m
              WHERE m.conversation_id = f.conversation_id AND m.author_type IN ('ai', 'human')
                AND NOT m.is_note AND NOT m.is_draft AND m.created_at >= f.first_in) r ON r.first_reply IS NOT NULL
    """),
        {"org": org_id, "since": since},
    )

    return DashboardOut(
        range_days=days,
        leads=sum(leads.values()),
        conversations=conversations,
        ai_handled=ai_convs,
        ai_resolution_rate=round((ai_convs - escalated) / ai_convs, 4) if ai_convs else 0.0,
        bookings=sum(booked.values()),
        revenue_minor=int(sum(v or 0 for v in revenue.values())),
        open_tasks=tasks[0],
        overdue_tasks=tasks[1],
        escalations_open=escalations_open,
        median_first_response_seconds=int(frt) if frt is not None else None,
        trend=trend,
    )

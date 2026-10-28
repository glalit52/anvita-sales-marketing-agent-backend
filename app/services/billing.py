"""Anvita's own plans, usage meters and credit balance for a workspace."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Conversation, CreditLedger, Message


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_minor: int
    who: str
    features: tuple[str, ...]
    ai_conversations: int
    prospect_credits: int
    voice_minutes: int
    wa_conversations: int
    users: int | None
    locations: int | None


PLANS = [
    Plan("trial", "Trial", 0, "14-day free trial", ("Everything in Growth for 14 days",), 200, 100, 0, 500, 3, 1),
    Plan(
        "starter",
        "Starter",
        2499_00,
        "Solo / single location",
        (
            "Unified inbox + webchat",
            "WhatsApp Business",
            "AI agent (Copilot)",
            "500 AI conversations / mo",
            "1 location, 3 users",
        ),
        500,
        0,
        0,
        1000,
        3,
        1,
    ),
    Plan(
        "growth",
        "Growth",
        7999_00,
        "SMB, multi-user",
        (
            "Everything in Starter",
            "AI Autopilot + booking",
            "Reviews, payments, campaigns",
            "2,000 AI conversations / mo",
            "1,000 prospecting credits",
            "Up to 3 locations, 10 users",
        ),
        2000,
        1000,
        100,
        5000,
        10,
        3,
    ),
    Plan(
        "scale",
        "Scale",
        19999_00,
        "Multi-location / B2B teams",
        (
            "Everything in Growth",
            "Sequences & Agent Studio evals",
            "CRM integrations + API",
            "6,000 AI conversations / mo",
            "500 voice minutes",
            "Unlimited locations",
        ),
        6000,
        5000,
        500,
        20000,
        30,
        None,
    ),
    Plan(
        "agency",
        "Agency",
        49999_00,
        "Agencies managing clients",
        (
            "Everything in Scale",
            "White-label & custom domain",
            "Client workspaces",
            "Rebilling & margins",
            "Priority support + CSM",
        ),
        20000,
        20000,
        2000,
        100000,
        None,
        None,
    ),
]
PLANS_BY_ID = {p.id: p for p in PLANS}

CREDIT_PACKS = {
    "ai500": ("500 AI conversations", "ai", 500, 1499_00),
    "prospect1000": ("1,000 prospecting credits", "prospect", 1000, 2999_00),
    "voice250": ("250 voice minutes", "voice", 250, 1999_00),
}


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_reset(now: datetime | None = None) -> datetime:
    start = month_start(now)
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


async def purchased(db: AsyncSession, org_id: uuid.UUID, meter: str, since: datetime) -> int:
    return int(
        await db.scalar(
            select(func.coalesce(func.sum(CreditLedger.delta), 0)).where(
                CreditLedger.org_id == org_id,
                CreditLedger.meter == meter,
                CreditLedger.delta > 0,
                CreditLedger.created_at >= since,
            )
        )
        or 0
    )


async def used(db: AsyncSession, org_id: uuid.UUID, meter: str, since: datetime) -> int:
    if meter == "ai":
        return int(
            await db.scalar(
                select(func.count(func.distinct(AgentRun.conversation_id))).where(
                    AgentRun.org_id == org_id,
                    AgentRun.kind == "reply",
                    AgentRun.outcome != "skipped",
                    AgentRun.created_at >= since,
                )
            )
            or 0
        )
    if meter == "wa":
        return int(
            await db.scalar(
                select(func.count(func.distinct(Message.conversation_id)))
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Message.org_id == org_id, Conversation.channel_type == "whatsapp", Message.created_at >= since)
            )
            or 0
        )
    return -int(
        await db.scalar(
            select(func.coalesce(func.sum(CreditLedger.delta), 0)).where(
                CreditLedger.org_id == org_id,
                CreditLedger.meter == meter,
                CreditLedger.delta < 0,
                CreditLedger.created_at >= since,
            )
        )
        or 0
    )


async def meters(db: AsyncSession, org_id: uuid.UUID, plan_id: str) -> list[dict]:
    plan = PLANS_BY_ID.get(plan_id, PLANS[0])
    since = month_start()
    defs = [
        ("ai", "AI conversations", plan.ai_conversations, ""),
        ("prospect", "Prospecting credits", plan.prospect_credits, ""),
        ("voice", "Voice minutes", plan.voice_minutes, "min"),
        ("wa", "WhatsApp conversations", plan.wa_conversations, ""),
    ]
    out = []
    for meter, label, included, unit in defs:
        limit = included + await purchased(db, org_id, meter, since)
        out.append(
            {
                "id": meter,
                "label": label,
                "used": await used(db, org_id, meter, since),
                "limit": limit,
                "unit": unit,
                "resetsAt": next_reset(),
            }
        )
    return out


async def remaining(db: AsyncSession, org_id: uuid.UUID, plan_id: str, meter: str) -> int:
    m = next(x for x in await meters(db, org_id, plan_id) if x["id"] == meter)
    return m["limit"] - m["used"]


def charge(
    db: AsyncSession, org_id: uuid.UUID, meter: str, amount: int, reason: str, ref_id: uuid.UUID | None = None
) -> None:
    """Record usage (call only on success: credits are charged for results, not attempts)."""
    if amount > 0:
        db.add(CreditLedger(org_id=org_id, meter=meter, delta=-amount, reason=reason, ref_id=ref_id))

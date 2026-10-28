"""Tools the agent can call. Business rules live here, never only in the prompt: every tool is
tenant-scoped server-side, validates its input and enforces the org's guardrails."""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.base import utcnow
from app.models import (
    AgentConfig,
    Appointment,
    Contact,
    Conversation,
    Location,
    Organization,
    Service,
    Task,
)
from app.services import knowledge, scheduling
from app.services.audit import audit
from app.services.payments import create_payment, pay_url


@dataclass
class ToolContext:
    db: AsyncSession
    org: Organization
    contact: Contact
    conversation: Conversation | None
    config: AgentConfig
    # Collected while the loop runs:
    sources: list[str] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    escalation: dict[str, str] | None = None
    # Sandbox runs execute tools against a rolled-back transaction and never notify anyone.
    sandbox: bool = False

    async def checkpoint(self) -> None:
        """Make tool side effects durable and release row locks before the next model call. Sandbox runs
        keep everything in one transaction so it can be rolled back."""
        if not self.sandbox:
            await self.db.commit()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.org.timezone or "UTC")


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.properties),
                "additionalProperties": False,
            },
        }


def _s(desc: str, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["string", "null"] if nullable else "string", "description": desc}


async def _find_by_name(db: AsyncSession, model: type, org_id: uuid.UUID, name: str | None):  # noqa: ANN202
    rows = list((await db.scalars(select(model).where(model.org_id == org_id, model.active.is_(True)))).all())
    if not rows:
        return None
    if not name:
        return rows[0] if len(rows) == 1 else None
    n = name.strip().lower()
    return next((r for r in rows if r.name.lower() == n), None) or next(
        (r for r in rows if n in r.name.lower() or r.name.lower() in n), None
    )


def _fmt_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _money(org: Organization, minor: int) -> str:
    symbol = "₹" if org.currency == "INR" else f"{org.currency} "
    return f"{symbol}{minor / 100:,.0f}" if minor % 100 == 0 else f"{symbol}{minor / 100:,.2f}"


# ---------------------------------------------------------------------------------------------------


async def search_knowledge(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    hits = await knowledge.search(ctx.db, ctx.org.id, args["query"], limit=4)
    for h in hits:
        if h.source_title not in ctx.sources:
            ctx.sources.append(h.source_title)
    return {"results": [{"source": h.source_title, "title": h.title, "content": h.content} for h in hits]}


async def get_contact(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    c = ctx.contact
    upcoming = (
        await ctx.db.scalars(
            select(Appointment)
            .where(
                Appointment.contact_id == c.id,
                Appointment.starts_at > utcnow(),
                Appointment.status.notin_(scheduling.INACTIVE_STATUSES),
            )
            .order_by(Appointment.starts_at)
            .limit(5)
        )
    ).all()
    services = {s.id: s.name for s in (await ctx.db.scalars(select(Service).where(Service.org_id == ctx.org.id))).all()}
    return {
        "name": c.name,
        "phone": c.phone,
        "email": c.email,
        "city": c.city,
        "language": c.language,
        "stage": c.stage,
        "tags": c.tags,
        "upcoming_appointments": [
            {
                "appointment_id": str(a.id),
                "service": services.get(a.service_id),
                "when": _fmt_local(a.starts_at, ctx.tz),
                "status": a.status,
            }
            for a in upcoming
        ],
    }


async def update_contact(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    c = ctx.contact
    changed = {}
    for key in ("name", "email", "city", "language"):
        if args.get(key):
            setattr(c, key, args[key].strip()[:200])
            changed[key] = args[key]
    if args.get("add_tags"):
        tags = [t.strip().lower()[:64] for t in args["add_tags"].split(",") if t.strip()]
        c.tags = list(dict.fromkeys([*c.tags, *tags]))
        changed["tags"] = tags
    return {"updated": changed}


async def check_availability(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    service = await _find_by_name(ctx.db, Service, ctx.org.id, args.get("service"))
    if service is None:
        names = [
            s.name
            for s in (
                await ctx.db.scalars(select(Service).where(Service.org_id == ctx.org.id, Service.online.is_(True)))
            ).all()
        ]
        return {"error": "Unknown service", "bookable_services": names}
    location = await _find_by_name(ctx.db, Location, ctx.org.id, args.get("location")) if args.get("location") else None
    try:
        start = date.fromisoformat(args["date_from"]) if args.get("date_from") else utcnow().astimezone(ctx.tz).date()
    except ValueError:
        return {"error": "date_from must be YYYY-MM-DD"}
    slots = await scheduling.availability(
        ctx.db,
        ctx.org,
        service,
        start=start,
        days=max(1, min(int(args.get("days") or 3), 14)),
        location_id=location.id if location else None,
        limit=8,
    )
    locs = {
        loc.id: loc.name for loc in (await ctx.db.scalars(select(Location).where(Location.org_id == ctx.org.id))).all()
    }
    return {
        "service": service.name,
        "duration_min": service.duration_min,
        "price": _money(ctx.org, service.price_minor) if service.price_minor else None,
        "deposit": _money(ctx.org, service.deposit_minor) if service.deposit_minor else None,
        "slots": [
            {
                "starts_at": s.starts_at.isoformat(),
                "label": _fmt_local(s.starts_at, ctx.tz),
                "location": locs.get(s.location_id),
            }
            for s in slots
        ],
    }


async def book_appointment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    service = await _find_by_name(ctx.db, Service, ctx.org.id, args.get("service"))
    location = await _find_by_name(ctx.db, Location, ctx.org.id, args.get("location"))
    if service is None or location is None:
        return {"error": "Unknown service or location; call check_availability first"}
    try:
        starts_at = datetime.fromisoformat(args["starts_at"])
    except ValueError:
        return {"error": "starts_at must be an ISO 8601 timestamp from check_availability"}
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=ctx.tz)
    key = f"ai:{ctx.conversation.id if ctx.conversation else ctx.contact.id}:{service.id}:{starts_at.isoformat()}"
    try:
        appt = await scheduling.book(
            ctx.db,
            ctx.org,
            contact_id=ctx.contact.id,
            service_id=service.id,
            location_id=location.id,
            starts_at=starts_at,
            source="ai",
            notes=args.get("notes"),
            idempotency_key=key,
            conversation_id=ctx.conversation.id if ctx.conversation else None,
        )
    except AppError as e:
        return {"error": e.message, "code": e.code}
    if ctx.contact.stage in ("prospect", "new", "qualified"):
        ctx.contact.stage = "booked"
    label = _fmt_local(appt.starts_at, ctx.tz)
    ctx.cards.append(
        {
            "type": "booking",
            "title": service.name,
            "subtitle": f"{label} · {location.name}",
            "status": "Confirmed",
            "appointmentId": str(appt.id),
        }
    )
    if not ctx.sandbox:
        audit(
            ctx.db,
            ctx.org.id,
            "appointment.booked",
            actor_type="ai",
            entity_type="appointment",
            entity_id=appt.id,
            data={"conversation_id": str(ctx.conversation.id) if ctx.conversation else None},
        )
    return {
        "booked": True,
        "appointment_id": str(appt.id),
        "when": label,
        "location": location.name,
        "deposit_required": _money(ctx.org, service.deposit_minor) if service.deposit_minor else None,
    }


async def cancel_appointment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        appt_id = uuid.UUID(args["appointment_id"])
    except ValueError:
        return {"error": "Invalid appointment id"}
    appt = await ctx.db.get(Appointment, appt_id)
    # The agent may only touch the current contact's own appointments.
    if appt is None or appt.contact_id != ctx.contact.id or appt.org_id != ctx.org.id:
        return {"error": "Appointment not found for this contact"}
    scheduling.cancel(ctx.db, appt, (args.get("reason") or "Cancelled by customer via AI")[:300])
    return {"cancelled": True, "appointment_id": str(appt.id)}


async def create_payment_link(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        amount_minor = round(float(args["amount"]) * 100)
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    limit = ctx.config.guardrails.get("max_payment_minor") or get_settings().max_ai_payment_amount
    if amount_minor <= 0:
        return {"error": "amount must be positive"}
    if amount_minor > limit:
        return {"error": f"Amount exceeds the AI limit of {_money(ctx.org, limit)}; hand off to a human"}
    if ctx.sandbox:  # never create real provider orders while testing
        return {"payment_url": "https://pay.example/sandbox", "amount": _money(ctx.org, amount_minor)}
    payment = await create_payment(
        ctx.db,
        ctx.org,
        amount_minor=amount_minor,
        purpose=args["purpose"][:200],
        contact=ctx.contact,
        conversation_id=ctx.conversation.id if ctx.conversation else None,
        created_by_type="ai",
    )
    url = pay_url(payment)
    ctx.cards.append(
        {
            "type": "payment",
            "title": f"{_money(ctx.org, amount_minor)} · {payment.purpose}",
            "subtitle": "Tap to pay securely",
            "status": "Pending",
            "paymentId": str(payment.id),
            "url": url,
        }
    )
    return {"payment_url": url, "amount": _money(ctx.org, amount_minor)}


async def create_task(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        due = datetime.fromisoformat(args["due_at"]) if args.get("due_at") else utcnow() + timedelta(days=1)
    except ValueError:
        return {"error": "due_at must be ISO 8601"}
    if due.tzinfo is None:
        due = due.replace(tzinfo=ctx.tz)
    ttype = args.get("type") if args.get("type") in ("call", "whatsapp", "email", "review", "payment") else "call"
    task = Task(
        org_id=ctx.org.id,
        title=args["title"][:300],
        type=ttype,
        contact_id=ctx.contact.id,
        conversation_id=ctx.conversation.id if ctx.conversation else None,
        assignee_id=ctx.contact.owner_id,
        created_by_type="ai",
        due_at=due,
        has_time=True,
        priority="normal",
        ai_context=args.get("note"),
    )
    ctx.db.add(task)
    await ctx.db.flush()
    return {"task_id": str(task.id), "due": _fmt_local(due, ctx.tz)}


async def handoff_to_human(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ctx.escalation = {"reason": args["reason"][:300], "summary": (args.get("summary") or "")[:2000]}
    return {
        "handed_off": True,
        "instruction": "Tell the customer a teammate will follow up shortly. Do not promise a time.",
    }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "search_knowledge",
            "Search the business's knowledge base (prices, services, policies, FAQs, hours). Use it before stating "
            "any business fact.",
            {"query": _s("What to look up, in the customer's words or as keywords")},
            search_knowledge,
        ),
        Tool("get_contact", "Read the current customer's profile and upcoming appointments.", {}, get_contact),
        Tool(
            "update_contact",
            "Save details the customer has told you. Pass null for anything not provided.",
            {
                "name": _s("Full name", True),
                "email": _s("Email address", True),
                "city": _s("City", True),
                "language": _s("Preferred language", True),
                "add_tags": _s("Comma-separated interest tags, e.g. 'whitening,wedding'", True),
            },
            update_contact,
        ),
        Tool(
            "check_availability",
            "List free appointment slots for a service. Always call this before offering times.",
            {
                "service": _s("Service name"),
                "location": _s("Location name, or null for any", True),
                "date_from": _s("First day to search, YYYY-MM-DD, or null for today", True),
                "days": {"type": ["integer", "null"], "description": "Days to search (1-14), null for 3"},
            },
            check_availability,
        ),
        Tool(
            "book_appointment",
            "Book a slot the customer has explicitly chosen. Use starts_at exactly as returned by check_availability.",
            {
                "service": _s("Service name"),
                "location": _s("Location name"),
                "starts_at": _s("Slot start, ISO 8601 from check_availability"),
                "notes": _s("Notes for the team", True),
            },
            book_appointment,
        ),
        Tool(
            "cancel_appointment",
            "Cancel one of the customer's own appointments when they ask to.",
            {"appointment_id": _s("Appointment id from get_contact"), "reason": _s("Reason given", True)},
            cancel_appointment,
        ),
        Tool(
            "create_payment_link",
            "Create a secure payment link (deposits, invoices). Only for amounts the business has defined.",
            {
                "amount": {"type": "number", "description": "Amount in major currency units, e.g. 500 for ₹500"},
                "purpose": _s("What the payment is for"),
            },
            create_payment_link,
        ),
        Tool(
            "create_task",
            "Create a follow-up task for the team, e.g. 'call back Tuesday'.",
            {
                "title": _s("Short task title"),
                "type": _s("call | whatsapp | email | review | payment", True),
                "due_at": _s("When it is due, ISO 8601, or null for tomorrow", True),
                "note": _s("Why, quoting the customer where useful", True),
            },
            create_task,
        ),
        Tool(
            "handoff_to_human",
            "Escalate to a human teammate: complaints, refunds, medical/legal questions, anything you cannot answer "
            "from the knowledge base, or when the customer asks for a person.",
            {"reason": _s("Short reason"), "summary": _s("Context so the teammate need not re-read the thread")},
            handoff_to_human,
        ),
    ]
}


def tool_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    return [t.schema() for n, t in TOOLS.items() if names is None or n in names]


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Execute a tool; returns (JSON result, is_error). Failures go back to the model, not up the stack."""
    tool = TOOLS.get(name)
    if tool is None:
        return json.dumps({"error": f"Unknown tool {name}"}), True
    try:
        result = await tool.handler(ctx, args or {})
    except (KeyError, TypeError) as e:
        result = {"error": f"Invalid arguments: {e}"}
    except AppError as e:  # e.g. payment provider down: tell the model, which can hand off
        result = {"error": e.message, "code": e.code}
    is_error = "error" in result
    ctx.calls.append(
        {"tool": name, "input": args, "ok": not is_error, "error": result.get("error") if is_error else None}
    )
    return json.dumps(result, default=str), is_error

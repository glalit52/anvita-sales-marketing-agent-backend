"""Agent runtime: decides whether to answer, runs the tool loop, applies guardrails, and records the run.

Flow for an inbound message (see docs/architecture.md):
  debounce → skip checks → opt-out / keyword / VIP rules → classify (intent, sentiment, summary)
  → sentiment / intent rules → tool-use loop → post-checks → send (autopilot) or draft (copilot)
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.base import utcnow
from app.models import (
    AgentConfig,
    AgentRun,
    Channel,
    Contact,
    Conversation,
    EscalationRule,
    Location,
    Message,
    Organization,
    Playbook,
    Service,
    User,
)
from app.services.agent.llm import LLMClient, LLMError, Usage, get_llm
from app.services.agent.prompts import (
    CLASSIFY_SCHEMA,
    CLASSIFY_SYSTEM,
    stable_system_prompt,
    volatile_context,
)
from app.services.agent.tools import ToolContext, run_tool, tool_schemas
from app.services.audit import audit
from app.services.contacts import set_consent
from app.services.events import emit
from app.services.inbox import create_outbound
from app.services.notify import notify

log = logging.getLogger(__name__)

OPT_OUT = {"stop", "unsubscribe", "stop all", "opt out", "optout", "cancel messages", "band karo"}
HANDOFF_TEXT = "Thanks for your patience. I'm bringing in a teammate who will get back to you shortly."


async def ensure_agent_config(db: AsyncSession, org: Organization) -> AgentConfig:
    config = await db.scalar(select(AgentConfig).where(AgentConfig.org_id == org.id))
    if config is None:
        config = AgentConfig(org_id=org.id, name="Anvita", languages=["English"], guardrails={}, instructions="")
        db.add(config)
        await db.flush()
    return config


def to_llm_messages(history: list[Message]) -> list[dict[str, Any]]:
    """Map the stored thread onto user/assistant turns. Notes, drafts and system events are omitted."""
    out: list[dict[str, Any]] = []
    for m in history:
        if m.is_note or m.is_draft or m.author_type == "system" or not m.body.strip():
            continue
        role = "user" if m.author_type == "contact" else "assistant"
        text = m.body if m.author_type != "human" else f"{m.body}"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += f"\n{text}"
        else:
            out.append({"role": role, "content": text})
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": "[The business started this conversation.]"})
    return out


@dataclass
class Generation:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    refused: bool = False
    error: str | None = None


async def generate(
    ctx: ToolContext,
    messages: list[dict[str, Any]],
    llm: LLMClient,
    *,
    channel: str,
) -> Generation:
    s = get_settings()
    playbooks = list(
        (
            await ctx.db.scalars(
                select(Playbook)
                .where(Playbook.org_id == ctx.org.id, Playbook.enabled.is_(True))
                .order_by(Playbook.priority, Playbook.name)
            )
        ).all()
    )
    playbooks = [p for p in playbooks if not p.channels or channel in p.channels]
    services = list(
        (
            await ctx.db.scalars(
                select(Service)
                .where(Service.org_id == ctx.org.id, Service.active.is_(True), Service.online.is_(True))
                .order_by(Service.name)
            )
        ).all()
    )
    locations = list(
        (
            await ctx.db.scalars(
                select(Location).where(Location.org_id == ctx.org.id, Location.active.is_(True)).order_by(Location.name)
            )
        ).all()
    )
    system = [
        {
            "type": "text",
            "text": stable_system_prompt(ctx.org, ctx.config, playbooks, services, locations),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": volatile_context(ctx.org, ctx.contact, channel, utcnow())},
    ]
    tools = tool_schemas()
    gen = Generation(text="", model=s.llm_conversation_model)
    convo = list(messages)
    for _ in range(s.agent_max_tool_steps):
        try:
            resp = await llm.complete(
                model=s.llm_conversation_model,
                system=system,
                messages=convo,
                tools=tools,
                max_tokens=4096,
                effort="medium",
            )
        except LLMError as e:
            gen.error = str(e)
            return gen
        gen.usage.add(resp.usage)
        gen.model = resp.model
        if resp.stop_reason == "refusal":
            gen.refused = True
            ctx.escalation = ctx.escalation or {"reason": "AI declined to answer", "summary": ""}
            return gen
        convo.append(resp.assistant_message())
        if not resp.tool_uses:
            gen.text = resp.text
            if resp.stop_reason == "max_tokens":
                gen.error = "Reply truncated (max_tokens)"
            return gen
        results = []
        for use in resp.tool_uses:
            content, is_error = await run_tool(ctx, use["name"], use["input"])
            results.append({"type": "tool_result", "tool_use_id": use["id"], "content": content, "is_error": is_error})
        convo.append({"role": "user", "content": results})
        await ctx.checkpoint()
    gen.error = "Tool loop limit reached"
    ctx.escalation = ctx.escalation or {"reason": "AI could not complete the request", "summary": ""}
    return gen


async def classify(
    db: AsyncSession, conv: Conversation, history: list[Message], llm: LLMClient
) -> tuple[dict[str, str], Usage]:
    lines = []
    for m in history[-20:]:
        if m.is_draft or not m.body.strip():
            continue
        who = {"contact": "Customer", "ai": "AI", "human": "Team", "system": "System"}[m.author_type]
        lines.append(f"{who}{' (internal note)' if m.is_note else ''}: {m.body}")
    transcript = "\n".join(lines)
    try:
        resp = await llm.complete(
            model=get_settings().llm_fast_model,
            system=[{"type": "text", "text": CLASSIFY_SYSTEM}],
            messages=[{"role": "user", "content": f"<transcript>\n{transcript}\n</transcript>"}],
            max_tokens=1024,
            effort="low",
            json_schema=CLASSIFY_SCHEMA,
        )
        data = json.loads(resp.text)
        usage = resp.usage
    except (LLMError, json.JSONDecodeError, ValueError) as e:
        log.warning("classification failed", extra={"data": {"error": str(e)}})
        return {}, Usage()
    conv.intent = str(data.get("intent", ""))[:120] or conv.intent
    if data.get("sentiment") in ("positive", "neutral", "negative"):
        conv.sentiment = data["sentiment"]
    conv.ai_summary = str(data.get("summary", ""))[:2000] or conv.ai_summary
    return data, usage


def match_rule(
    rules: list[EscalationRule], *, text: str, contact: Contact, labels: dict[str, str], phase: str
) -> EscalationRule | None:
    lowered = f" {text.lower()} "
    for r in rules:
        cond = r.condition or {}
        if phase == "pre" and r.kind == "keyword":
            hit = any(k.lower() in lowered for k in cond.get("keywords", []) if k)
        elif phase == "pre" and r.kind == "vip":
            hit = bool({t.lower() for t in cond.get("tags", ["vip"])} & {t.lower() for t in contact.tags})
        elif phase == "post" and r.kind == "sentiment":
            hit = bool(labels.get("sentiment")) and labels["sentiment"] == cond.get("sentiment", "negative")
        elif phase == "post" and r.kind == "intent":
            intent = (labels.get("intent") or "").lower()
            hit = bool(intent) and any(i.lower() in intent for i in cond.get("intents", []))
        else:
            hit = False
        if hit:
            return r
    return None


async def escalate(
    db: AsyncSession,
    org: Organization,
    conv: Conversation,
    contact: Contact,
    *,
    reason: str,
    summary: str | None = None,
    rule: EscalationRule | None = None,
) -> None:
    action = rule.action if rule else "assign"
    assignee = (rule.assign_to_id if rule else None) or contact.owner_id
    if action == "assign":
        conv.assignee_type = "human"
        conv.assignee_id = assignee
    elif action == "pause":
        conv.ai_paused_until = utcnow() + timedelta(minutes=get_settings().agent_human_pause_minutes)
    conv.escalation_reason = reason
    emit(
        db,
        org.id,
        "conversation.escalated",
        {"conversation_id": conv.id, "contact_id": contact.id, "reason": reason, "assignee_id": assignee},
    )
    if summary:
        conv.ai_summary = summary
    conv.status = "open"
    name = None
    if assignee:
        user = await db.get(User, assignee)
        name = user.name if user else None
    await create_outbound(
        db, conv, body=f"Escalated{f' to {name}' if name else ' to the team'}: {reason}", author_type="system"
    )
    notify(
        db,
        org.id,
        tab="escalations",
        icon="escalation",
        title=f"{contact.name} needs a human",
        body=reason,
        user_id=assignee,
        contact_id=contact.id,
        link=f"/inbox?c={conv.id}",
        priority=True,
    )


async def run_reply(
    db: AsyncSession,
    org_id: uuid.UUID,
    conversation_id: uuid.UUID,
    trigger_message_id: uuid.UUID,
    *,
    llm: LLMClient | None = None,
) -> AgentRun | None:
    """Handle one `message.received` event.

    Commits at checkpoints (after classification and after each tool round) so no locks are held while waiting
    on the model; the caller commits the final reply.
    """
    s = get_settings()
    llm = llm or get_llm()
    started = time.monotonic()
    # No row lock here: it would be held across LLM calls and block the webhook storing the customer's next
    # message. Concurrent runs for one thread are resolved by the debounce check and idempotency keys.
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        return None
    org = await db.get(Organization, org_id)
    contact = await db.get(Contact, conv.contact_id)
    assert org is not None and contact is not None
    config = await ensure_agent_config(db, org)
    channel = await db.get(Channel, conv.channel_id) if conv.channel_id else None
    mode = (
        channel.agent_mode
        if channel
        else ((config.guardrails or {}).get("channel_modes") or {}).get(conv.channel_type, config.default_mode)
    )

    history = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(s.agent_history_messages)
            )
        ).all()
    )[::-1]
    latest_inbound = next((m for m in reversed(history) if m.author_type == "contact"), None)

    def record(outcome: str, reason: str | None = None, **kw: Any) -> AgentRun:
        run = AgentRun(
            org_id=org.id,
            conversation_id=conv.id,
            trigger_message_id=trigger_message_id,
            kind="reply",
            model=kw.pop("model", "none"),
            mode=mode,
            outcome=outcome,
            reason=reason,
            latency_ms=int((time.monotonic() - started) * 1000),
            tool_calls=kw.pop("tool_calls", []),
            **kw,
        )
        db.add(run)
        emit(
            db,
            org.id,
            "agent_run.completed",
            {"conversation_id": conv.id, "outcome": outcome, "reason": reason, "model": run.model},
        )
        return run

    # --- Should the AI speak at all? -------------------------------------------------------------
    if latest_inbound is None or latest_inbound.id != trigger_message_id:
        return None  # debounce: a newer inbound message has its own event
    if history and history[-1].author_type in ("ai", "human") and not history[-1].is_note and not history[-1].is_draft:
        return None  # already answered
    if conv.assignee_type != "ai":
        return record("skipped", "Assigned to a human")
    if conv.ai_paused_until and conv.ai_paused_until > utcnow():
        return record("skipped", "AI paused: a teammate is handling this thread")
    if mode == "off":
        return record("skipped", "AI is off for this channel")

    text = latest_inbound.body.strip()
    if text.lower().strip(" .!") in OPT_OUT:
        if conv.channel_type in ("whatsapp", "sms", "email"):
            await set_consent(
                db,
                org.id,
                contact.id,
                channel=conv.channel_type,
                status="opted_out",
                source=f"Keyword '{text}' on {conv.channel_type}",
            )
        await create_outbound(
            db,
            conv,
            body="You're unsubscribed and won't get marketing messages from us. Reply START anytime to opt back in.",
            author_type="ai",
        )
        audit(db, org.id, "consent.opted_out", actor_type="ai", entity_type="contact", entity_id=contact.id)
        return record("replied", "Opt-out keyword")

    rules = list(
        (
            await db.scalars(
                select(EscalationRule).where(EscalationRule.org_id == org.id, EscalationRule.enabled.is_(True))
            )
        ).all()
    )
    recent_text = " ".join(m.body for m in history[-4:] if m.author_type == "contact")
    rule = match_rule(rules, text=recent_text, contact=contact, labels={}, phase="pre")
    labels, usage = await classify(db, conv, history, llm)
    if rule is None:
        rule = match_rule(rules, text=recent_text, contact=contact, labels=labels, phase="post")
    if rule is not None and rule.action != "notify":
        await create_outbound(db, conv, body=HANDOFF_TEXT, author_type="ai")
        await escalate(db, org, conv, contact, reason=f"{rule.name}", summary=labels.get("summary"), rule=rule)
        return record(
            "escalated",
            rule.name,
            model=s.llm_fast_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost(s.llm_fast_model),
        )
    if rule is not None:  # notify-only rule: tell the team, keep the AI answering
        notify(
            db,
            org.id,
            tab="escalations",
            icon="escalation",
            title=f"{rule.name}: {contact.name}",
            body=text[:300],
            user_id=rule.assign_to_id,
            contact_id=contact.id,
            link=f"/inbox?c={conv.id}",
        )

    # --- Generate --------------------------------------------------------------------------------
    await db.commit()
    ctx = ToolContext(db=db, org=org, contact=contact, conversation=conv, config=config)
    gen = await generate(ctx, to_llm_messages(history), llm, channel=conv.channel_type)
    usage.add(gen.usage)
    run_id = uuid.uuid4()
    base = dict(
        model=gen.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost(gen.model),
        tool_calls=ctx.calls,
    )

    if gen.error and not ctx.escalation and not gen.text:
        await escalate(db, org, conv, contact, reason="AI error, needs a human", summary=labels.get("summary"))
        run = record("error", gen.error, error=gen.error, **base)
        run.id = run_id
        return run

    reply = gen.text or (HANDOFF_TEXT if ctx.escalation else "")
    payment_cards = [c for c in ctx.cards if c["type"] == "payment"]
    for c in payment_cards:
        if c["url"] not in reply:
            reply = f"{reply}\n{c['url']}".strip()
    card = next((c for c in ctx.cards if c["type"] == "booking"), None) or (payment_cards[0] if payment_cards else None)

    outcome = "replied"
    if reply:
        if mode == "copilot" and not ctx.escalation:
            await create_outbound(
                db,
                conv,
                body=reply,
                author_type="ai",
                author_name=config.name,
                is_draft=True,
                card=card,
                sources=ctx.sources,
                ai_run_id=run_id,
                idempotency_key=f"ai-reply:{trigger_message_id}",
            )
            notify(
                db,
                org.id,
                tab="ai",
                icon="ai",
                title=f"Draft reply ready for {contact.name}",
                body=reply[:300],
                user_id=contact.owner_id,
                contact_id=contact.id,
                link=f"/inbox?c={conv.id}",
            )
            outcome = "drafted"
        else:
            await create_outbound(
                db,
                conv,
                body=reply,
                author_type="ai",
                author_name=config.name,
                card=card,
                sources=ctx.sources,
                ai_run_id=run_id,
                idempotency_key=f"ai-reply:{trigger_message_id}",
            )
    if ctx.escalation:
        await escalate(
            db,
            org,
            conv,
            contact,
            reason=ctx.escalation["reason"],
            summary=ctx.escalation.get("summary") or labels.get("summary"),
        )
        outcome = "escalated"
    conv.unread_count = 0 if outcome == "replied" else conv.unread_count
    run = record(outcome, ctx.escalation["reason"] if ctx.escalation else None, error=gen.error, **base)
    run.id = run_id
    return run


async def sandbox_reply(
    db: AsyncSession,
    org: Organization,
    transcript: list[dict[str, str]],
    *,
    channel: str = "webchat",
    llm: LLMClient | None = None,
) -> tuple[Generation, ToolContext]:
    """Run the agent on a scripted conversation inside a transaction that the caller rolls back, so
    bookings, payment links and tasks the agent creates while testing never persist."""
    config = await ensure_agent_config(db, org)
    contact = Contact(
        id=uuid.uuid4(), org_id=org.id, name="Sandbox visitor", stage="new", tags=[], utm={}, attributes={}
    )
    db.add(contact)
    await db.flush()
    ctx = ToolContext(db=db, org=org, contact=contact, conversation=None, config=config, sandbox=True)
    messages: list[dict[str, Any]] = []
    for turn in transcript:
        role = "user" if turn["role"] == "contact" else "assistant"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + turn["body"]
        else:
            messages.append({"role": role, "content": turn["body"]})
    if messages and messages[0]["role"] == "assistant":
        messages.insert(0, {"role": "user", "content": "[The business started this conversation.]"})
    gen = await generate(ctx, messages, llm or get_llm(), channel=channel)
    return gen, ctx


async def suggest_reply(
    db: AsyncSession, org: Organization, conv: Conversation, *, llm: LLMClient | None = None
) -> tuple[Generation, ToolContext]:
    """Copilot "suggest": run the agent on the real thread in sandbox mode (the caller rolls back)."""
    s = get_settings()
    contact = await db.get(Contact, conv.contact_id)
    assert contact is not None
    config = await ensure_agent_config(db, org)
    history = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(s.agent_history_messages)
            )
        ).all()
    )[::-1]
    messages = to_llm_messages(history)
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "[Internal: draft the business's next message to the customer.]"})
    ctx = ToolContext(db=db, org=org, contact=contact, conversation=conv, config=config, sandbox=True)
    gen = await generate(ctx, messages, llm or get_llm(), channel=conv.channel_type)
    return gen, ctx

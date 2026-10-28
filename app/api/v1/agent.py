import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DB, AdminPrincipal, CurrentPrincipal, ManagerPrincipal
from app.core.errors import AppError, NotFound
from app.db.base import utcnow
from app.models import AgentRun, AgentVersion, Appointment, EscalationRule, KnowledgeSource, Playbook, ScenarioTest
from app.schemas.agent import (
    AgentConfigOut,
    AgentConfigUpdate,
    AgentRunOut,
    AgentStats,
    AgentVersionOut,
    EscalationRuleIn,
    EscalationRuleOut,
    FaqIn,
    Guardrails,
    KnowledgeHit,
    KnowledgeSourceIn,
    KnowledgeSourceOut,
    PlaybookIn,
    PlaybookOut,
    PublishIn,
    SandboxIn,
    SandboxOut,
)
from app.schemas.common import Ok
from app.schemas.marketing import ChannelModeIn, ScenarioIn, ScenarioOut
from app.services import knowledge
from app.services.agent.runtime import ensure_agent_config, sandbox_reply
from app.services.audit import audit
from app.services.events import emit

router = APIRouter(prefix="/agent", tags=["agent"])


def _config_out(c) -> AgentConfigOut:  # noqa: ANN001
    return AgentConfigOut(
        name=c.name,
        tone=c.tone,
        instructions=c.instructions,
        languages=c.languages,
        default_mode=c.default_mode,
        guardrails=Guardrails.model_validate(c.guardrails or {}),
        version=c.version,
        updated_at=c.updated_at,
    )


@router.get("/config", response_model=AgentConfigOut)
async def get_config(principal: CurrentPrincipal, db: DB) -> AgentConfigOut:
    config = await ensure_agent_config(db, principal.org)
    await db.commit()
    return _config_out(config)


@router.patch("/config", response_model=AgentConfigOut)
async def update_config(body: AgentConfigUpdate, principal: ManagerPrincipal, db: DB) -> AgentConfigOut:
    config = await ensure_agent_config(db, principal.org)
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    if "guardrails" in changes:
        changes["guardrails"] = body.guardrails.model_dump(by_alias=False) if body.guardrails else {}
        # Per-channel modes live alongside the guardrails but are managed by PUT /agent/channel-modes.
        if (config.guardrails or {}).get("channel_modes"):
            changes["guardrails"]["channel_modes"] = config.guardrails["channel_modes"]
    for k, v in changes.items():
        setattr(config, k, v)
    audit(db, principal.org_id, "agent.config_updated", actor_id=principal.user_id, data={"fields": sorted(changes)})
    await db.commit()
    return _config_out(config)


@router.post("/publish", response_model=AgentVersionOut, status_code=201)
async def publish(body: PublishIn, principal: ManagerPrincipal, db: DB) -> AgentVersionOut:
    """Snapshot the current prompt, guardrails and playbooks as a numbered version (for rollback and audit)."""
    config = await ensure_agent_config(db, principal.org)
    playbooks = (await db.scalars(select(Playbook).where(Playbook.org_id == principal.org_id))).all()
    snapshot = {
        "config": {
            "name": config.name,
            "tone": config.tone,
            "instructions": config.instructions,
            "languages": config.languages,
            "default_mode": config.default_mode,
            "guardrails": config.guardrails,
        },
        "playbooks": [
            {
                "name": p.name,
                "triggers": p.triggers,
                "instructions": p.instructions,
                "tools": p.tools,
                "channels": p.channels,
                "enabled": p.enabled,
                "priority": p.priority,
            }
            for p in playbooks
        ],
    }
    latest = await db.scalar(select(func.max(AgentVersion.version)).where(AgentVersion.org_id == principal.org_id))
    v = AgentVersion(
        org_id=principal.org_id,
        version=(latest or 0) + 1,
        snapshot=snapshot,
        note=body.note,
        created_by_id=principal.user_id,
    )
    config.version = v.version
    db.add(v)
    await db.commit()
    return AgentVersionOut.model_validate(v)


@router.get("/versions", response_model=list[AgentVersionOut])
async def versions(principal: CurrentPrincipal, db: DB) -> list[AgentVersionOut]:
    rows = await db.scalars(
        select(AgentVersion).where(AgentVersion.org_id == principal.org_id).order_by(AgentVersion.version.desc())
    )
    return [AgentVersionOut.model_validate(v) for v in rows]


@router.post("/versions/{version}/restore", response_model=AgentConfigOut)
async def restore(version: int, principal: AdminPrincipal, db: DB) -> AgentConfigOut:
    v = await db.scalar(
        select(AgentVersion).where(AgentVersion.org_id == principal.org_id, AgentVersion.version == version)
    )
    if v is None:
        raise NotFound("Version not found")
    config = await ensure_agent_config(db, principal.org)
    for k, val in v.snapshot["config"].items():
        setattr(config, k, val)
    for p in (await db.scalars(select(Playbook).where(Playbook.org_id == principal.org_id))).all():
        await db.delete(p)
    for p in v.snapshot["playbooks"]:
        db.add(Playbook(org_id=principal.org_id, **p))
    config.version = v.version
    audit(db, principal.org_id, "agent.version_restored", actor_id=principal.user_id, data={"version": version})
    await db.commit()
    return _config_out(config)


# ---- Knowledge -------------------------------------------------------------------------------------


@router.get("/knowledge", response_model=list[KnowledgeSourceOut])
async def list_sources(principal: CurrentPrincipal, db: DB) -> list[KnowledgeSourceOut]:
    rows = await db.scalars(
        select(KnowledgeSource)
        .where(KnowledgeSource.org_id == principal.org_id)
        .order_by(KnowledgeSource.created_at.desc())
    )
    return [KnowledgeSourceOut.model_validate(r) for r in rows]


@router.post("/knowledge", response_model=KnowledgeSourceOut, status_code=202)
async def add_source(body: KnowledgeSourceIn, principal: ManagerPrincipal, db: DB) -> KnowledgeSourceOut:
    """Add a website (crawled in the background), or a text/FAQ document (indexed in the background)."""
    src = KnowledgeSource(
        org_id=principal.org_id,
        kind=body.kind,
        title=body.title,
        url=str(body.url) if body.url else None,
        content=body.content,
        status="queued",
    )
    db.add(src)
    await db.flush()
    emit(db, principal.org_id, "knowledge_source.created", {"source_id": src.id})
    await db.commit()
    return KnowledgeSourceOut.model_validate(src)


@router.post("/knowledge/faqs", response_model=KnowledgeSourceOut, status_code=201)
async def add_faq(body: FaqIn, principal: ManagerPrincipal, db: DB) -> KnowledgeSourceOut:
    src = await knowledge.add_faq(db, principal.org_id, body.question, body.answer)
    await db.commit()
    return KnowledgeSourceOut.model_validate(src)


@router.post("/knowledge/{source_id}/resync", response_model=KnowledgeSourceOut, status_code=202)
async def resync(source_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> KnowledgeSourceOut:
    src = await db.get(KnowledgeSource, source_id)
    if src is None:
        raise NotFound("Source not found")
    src.status = "queued"
    emit(db, principal.org_id, "knowledge_source.created", {"source_id": src.id})
    await db.commit()
    return KnowledgeSourceOut.model_validate(src)


@router.delete("/knowledge/{source_id}", response_model=Ok)
async def delete_source(source_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    src = await db.get(KnowledgeSource, source_id)
    if src is None:
        raise NotFound("Source not found")
    await db.delete(src)
    await db.commit()
    return Ok()


@router.get("/knowledge/search", response_model=list[KnowledgeHit])
async def search(
    principal: CurrentPrincipal, db: DB, q: str = Query(min_length=1), limit: int = Query(5, le=20)
) -> list[KnowledgeHit]:
    hits = await knowledge.search(db, principal.org_id, q, limit=limit)
    return [
        KnowledgeHit(
            source_id=h.source_id, source_title=h.source_title, title=h.title, content=h.content, score=h.score
        )
        for h in hits
    ]


# ---- Playbooks and escalation rules ----------------------------------------------------------------


@router.get("/playbooks", response_model=list[PlaybookOut])
async def list_playbooks(principal: CurrentPrincipal, db: DB) -> list[PlaybookOut]:
    rows = await db.scalars(
        select(Playbook).where(Playbook.org_id == principal.org_id).order_by(Playbook.priority, Playbook.name)
    )
    return [PlaybookOut.model_validate(r) for r in rows]


@router.post("/playbooks", response_model=PlaybookOut, status_code=201)
async def create_playbook(body: PlaybookIn, principal: ManagerPrincipal, db: DB) -> PlaybookOut:
    p = Playbook(org_id=principal.org_id, **body.model_dump(by_alias=False))
    db.add(p)
    await db.commit()
    return PlaybookOut.model_validate(p)


@router.put("/playbooks/{playbook_id}", response_model=PlaybookOut)
async def update_playbook(playbook_id: uuid.UUID, body: PlaybookIn, principal: ManagerPrincipal, db: DB) -> PlaybookOut:
    p = await db.get(Playbook, playbook_id)
    if p is None:
        raise NotFound("Playbook not found")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(p, k, v)
    await db.commit()
    return PlaybookOut.model_validate(p)


@router.delete("/playbooks/{playbook_id}", response_model=Ok)
async def delete_playbook(playbook_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    p = await db.get(Playbook, playbook_id)
    if p is None:
        raise NotFound("Playbook not found")
    await db.delete(p)
    await db.commit()
    return Ok()


@router.get("/escalation-rules", response_model=list[EscalationRuleOut])
async def list_rules(principal: CurrentPrincipal, db: DB) -> list[EscalationRuleOut]:
    rows = await db.scalars(
        select(EscalationRule).where(EscalationRule.org_id == principal.org_id).order_by(EscalationRule.name)
    )
    return [EscalationRuleOut.model_validate(r) for r in rows]


@router.post("/escalation-rules", response_model=EscalationRuleOut, status_code=201)
async def create_rule(body: EscalationRuleIn, principal: ManagerPrincipal, db: DB) -> EscalationRuleOut:
    r = EscalationRule(org_id=principal.org_id, **body.model_dump(by_alias=False))
    db.add(r)
    await db.commit()
    return EscalationRuleOut.model_validate(r)


@router.put("/escalation-rules/{rule_id}", response_model=EscalationRuleOut)
async def update_rule(
    rule_id: uuid.UUID, body: EscalationRuleIn, principal: ManagerPrincipal, db: DB
) -> EscalationRuleOut:
    r = await db.get(EscalationRule, rule_id)
    if r is None:
        raise NotFound("Rule not found")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(r, k, v)
    await db.commit()
    return EscalationRuleOut.model_validate(r)


@router.delete("/escalation-rules/{rule_id}", response_model=Ok)
async def delete_rule(rule_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    r = await db.get(EscalationRule, rule_id)
    if r is None:
        raise NotFound("Rule not found")
    await db.delete(r)
    await db.commit()
    return Ok()


# ---- Sandbox, runs, stats --------------------------------------------------------------------------


@router.post("/sandbox", response_model=SandboxOut)
async def sandbox(body: SandboxIn, principal: CurrentPrincipal, db: DB) -> SandboxOut:
    """Try the agent on a scripted conversation. Runs real tools, then rolls everything back."""
    gen, ctx = await sandbox_reply(db, principal.org, [m.model_dump() for m in body.messages], channel=body.channel)
    out = SandboxOut(
        reply=gen.text or None,
        escalated=ctx.escalation is not None,
        escalation_reason=ctx.escalation["reason"] if ctx.escalation else None,
        sources=ctx.sources,
        tool_calls=ctx.calls,
    )
    await db.rollback()
    return out


@router.get("/runs", response_model=list[AgentRunOut])
async def runs(
    principal: CurrentPrincipal,
    db: DB,
    conversation_id: uuid.UUID | None = None,
    outcome: str | None = None,
    limit: int = Query(50, le=200),
) -> list[AgentRunOut]:
    q = select(AgentRun).where(AgentRun.org_id == principal.org_id)
    if conversation_id:
        q = q.where(AgentRun.conversation_id == conversation_id)
    if outcome:
        q = q.where(AgentRun.outcome == outcome)
    rows = await db.scalars(q.order_by(AgentRun.created_at.desc()).limit(limit))
    return [AgentRunOut.model_validate(r) for r in rows]


@router.get("/stats", response_model=AgentStats)
async def stats(principal: CurrentPrincipal, db: DB, days: int = Query(30, ge=1, le=365)) -> AgentStats:
    since = utcnow() - timedelta(days=days)
    row = (
        await db.execute(
            select(
                func.count(func.distinct(AgentRun.conversation_id)),
                func.count().filter(AgentRun.outcome == "replied"),
                func.count().filter(AgentRun.outcome == "escalated"),
                func.coalesce(func.avg(AgentRun.latency_ms).filter(AgentRun.outcome.in_(["replied", "drafted"])), 0),
                func.coalesce(func.sum(AgentRun.cost_usd), 0),
            ).where(AgentRun.org_id == principal.org_id, AgentRun.created_at >= since)
        )
    ).one()
    escalated_convs = (
        await db.scalar(
            select(func.count(func.distinct(AgentRun.conversation_id))).where(
                AgentRun.org_id == principal.org_id, AgentRun.created_at >= since, AgentRun.outcome == "escalated"
            )
        )
        or 0
    )
    bookings = (
        await db.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(Appointment.org_id == principal.org_id, Appointment.source == "ai", Appointment.created_at >= since)
        )
        or 0
    )
    convs = row[0] or 0
    return AgentStats(
        conversations=convs,
        ai_resolved=max(convs - escalated_convs, 0),
        escalated=escalated_convs,
        resolution_rate=round((convs - escalated_convs) / convs, 4) if convs else 0.0,
        bookings_by_ai=bookings,
        avg_latency_ms=int(row[3]),
        cost_usd=Decimal(row[4]),
    )


# ---- Scenario tests, QA queue, knowledge gaps, channel modes ----------------------------------------


async def _run_scenario(
    db: DB, principal: CurrentPrincipal, scenario_id: uuid.UUID, messages: list, channel: str, exp: dict
) -> None:
    gen, ctx = await sandbox_reply(db, principal.org, messages, channel=channel)
    reply = gen.text or ""
    tools_called = [c.get("tool") for c in ctx.calls]
    escalated = ctx.escalation is not None
    await db.rollback()  # the sandbox's bookings / tasks never persist
    await db.refresh(principal.org)  # rollback expired it; later scenarios need it loaded
    detail = []
    for phrase in exp.get("mustInclude") or []:
        detail.append({"check": f"Mentions “{phrase}”", "pass": phrase.lower() in reply.lower()})
    for phrase in exp.get("mustNotInclude") or []:
        detail.append({"check": f"Never says “{phrase}”", "pass": phrase.lower() not in reply.lower()})
    if exp.get("tool"):
        detail.append({"check": f"Calls {exp['tool']}", "pass": exp["tool"] in tools_called})
    if "escalate" in exp:
        detail.append(
            {
                "check": "Escalates" if exp["escalate"] else "Doesn't escalate",
                "pass": escalated == bool(exp["escalate"]),
            }
        )
    if not reply and not escalated:
        detail.append({"check": "Replies", "pass": False})
    t = await db.get(ScenarioTest, scenario_id)
    assert t is not None
    t.last_reply, t.last_detail, t.last_run_at = reply, detail, utcnow()
    t.last_status = "pass" if all(d["pass"] for d in detail) else "fail"


@router.get("/scenarios", response_model=list[ScenarioOut])
async def list_scenarios(principal: CurrentPrincipal, db: DB) -> list[ScenarioOut]:
    rows = await db.scalars(
        select(ScenarioTest).where(ScenarioTest.org_id == principal.org_id).order_by(ScenarioTest.created_at)
    )
    return [ScenarioOut.model_validate(r) for r in rows]


@router.post("/scenarios", response_model=ScenarioOut, status_code=201)
async def create_scenario(body: ScenarioIn, principal: ManagerPrincipal, db: DB) -> ScenarioOut:
    t = ScenarioTest(org_id=principal.org_id, last_detail=[], **body.model_dump(by_alias=False))
    db.add(t)
    await db.commit()
    return ScenarioOut.model_validate(t)


@router.put("/scenarios/{scenario_id}", response_model=ScenarioOut)
async def update_scenario(scenario_id: uuid.UUID, body: ScenarioIn, principal: ManagerPrincipal, db: DB) -> ScenarioOut:
    t = await db.get(ScenarioTest, scenario_id)
    if t is None:
        raise NotFound("Scenario not found")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(t, k, v)
    await db.commit()
    return ScenarioOut.model_validate(t)


@router.delete("/scenarios/{scenario_id}", response_model=Ok)
async def delete_scenario(scenario_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    t = await db.get(ScenarioTest, scenario_id)
    if t is None:
        raise NotFound("Scenario not found")
    await db.delete(t)
    await db.commit()
    return Ok()


@router.post("/scenarios/run", response_model=list[ScenarioOut])
async def run_scenarios(principal: ManagerPrincipal, db: DB, scenario_id: uuid.UUID | None = None) -> list[ScenarioOut]:
    """Replay scenario conversations against the current agent config (e.g. before publishing)."""
    q = select(ScenarioTest).where(ScenarioTest.org_id == principal.org_id)
    if scenario_id:
        q = q.where(ScenarioTest.id == scenario_id)
    tests = [
        (t.id, list(t.messages), t.channel, dict(t.expect or {}))
        for t in (await db.scalars(q.order_by(ScenarioTest.created_at).limit(50))).all()
    ]
    ids = [t[0] for t in tests]
    await db.commit()
    for t in tests:
        await _run_scenario(db, principal, *t)
        await db.commit()
    rows = await db.scalars(select(ScenarioTest).where(ScenarioTest.id.in_(ids)).order_by(ScenarioTest.created_at))
    return [ScenarioOut.model_validate(r) for r in rows]


@router.get("/qa", response_model=list[dict])
async def qa_queue(
    principal: CurrentPrincipal, db: DB, reviewed: bool = False, limit: int = Query(30, le=100)
) -> list[dict]:
    """Recent AI replies for a human to spot-check, with the customer message they answered."""
    from app.models import Contact, Conversation, Message

    q = (
        select(AgentRun, Message.body, Contact.name, Conversation.channel_type)
        .outerjoin(Message, Message.id == AgentRun.trigger_message_id)
        .outerjoin(Conversation, Conversation.id == AgentRun.conversation_id)
        .outerjoin(Contact, Contact.id == Conversation.contact_id)
        .where(AgentRun.org_id == principal.org_id, AgentRun.outcome.in_(["replied", "drafted"]))
        .where(AgentRun.qa.is_not(None) if reviewed else AgentRun.qa.is_(None))
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
    )
    out = []
    for run, question, contact, channel in await db.execute(q):
        reply = await db.scalar(
            select(Message.body)
            .where(
                Message.conversation_id == run.conversation_id,
                Message.author_type == "ai",
                Message.created_at >= run.created_at,
            )
            .order_by(Message.created_at)
            .limit(1)
        )
        out.append(
            {
                "runId": str(run.id),
                "conversationId": str(run.conversation_id) if run.conversation_id else None,
                "contact": contact,
                "channel": channel,
                "question": question,
                "reply": reply,
                "tools": [c.get("tool") or c.get("name") for c in run.tool_calls or []],
                "qa": run.qa,
                "createdAt": run.created_at.isoformat(),
            }
        )
    return out


@router.post("/qa/{run_id}", response_model=Ok)
async def rate_reply(run_id: uuid.UUID, body: dict, principal: CurrentPrincipal, db: DB) -> Ok:
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise NotFound("Run not found")
    rating = body.get("rating")
    if rating not in ("good", "bad"):
        raise AppError("rating must be good or bad", code="invalid_rating")
    run.qa = {
        "rating": rating,
        "note": (body.get("note") or "")[:1000],
        "by": str(principal.user_id),
        "at": utcnow().isoformat(),
    }
    if rating == "bad" and body.get("correctAnswer") and body.get("question"):
        await knowledge.add_faq(db, principal.org_id, str(body["question"])[:500], str(body["correctAnswer"])[:4000])
    await db.commit()
    return Ok()


@router.get("/knowledge-gaps", response_model=list[dict])
async def knowledge_gaps(principal: CurrentPrincipal, db: DB) -> list[dict]:
    from app.services.analytics import knowledge_gaps as gaps

    return [{**g, "lastAsked": g["lastAsked"].isoformat()} for g in await gaps(db, principal.org_id)]


@router.post("/knowledge-gaps/answer", response_model=KnowledgeSourceOut, status_code=201)
async def answer_gap(body: dict, principal: ManagerPrincipal, db: DB) -> KnowledgeSourceOut:
    q, a = str(body.get("question") or "").strip(), str(body.get("answer") or "").strip()
    if not q or not a:
        raise AppError("question and answer are required", code="invalid_faq")
    src = await knowledge.add_faq(db, principal.org_id, q[:500], a[:4000])
    await db.commit()
    return KnowledgeSourceOut.model_validate(src)


@router.get("/channel-modes", response_model=dict)
async def channel_modes(principal: CurrentPrincipal, db: DB) -> dict:
    from app.models import Channel

    config = await ensure_agent_config(db, principal.org)
    modes = {c: config.default_mode for c in ("whatsapp", "sms", "email", "webchat", "instagram", "facebook")}
    modes.update((config.guardrails or {}).get("channel_modes") or {})
    for ch in (await db.scalars(select(Channel).where(Channel.org_id == principal.org_id))).all():
        modes[ch.type] = ch.agent_mode
    await db.commit()
    return modes


@router.put("/channel-modes", response_model=dict)
async def put_channel_modes(body: ChannelModeIn, principal: ManagerPrincipal, db: DB) -> dict:
    from app.models import Channel

    config = await ensure_agent_config(db, principal.org)
    config.guardrails = {
        **(config.guardrails or {}),
        "channel_modes": {**((config.guardrails or {}).get("channel_modes") or {}), **body.modes},
    }
    for ch in (await db.scalars(select(Channel).where(Channel.org_id == principal.org_id))).all():
        if ch.type in body.modes:
            ch.agent_mode = body.modes[ch.type]
    audit(db, principal.org_id, "agent.channel_modes", actor_id=principal.user_id, data=body.modes)
    await db.commit()
    return await channel_modes(principal, db)

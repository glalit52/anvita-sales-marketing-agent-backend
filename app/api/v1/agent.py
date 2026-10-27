import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DB, AdminPrincipal, CurrentPrincipal, ManagerPrincipal
from app.core.errors import NotFound
from app.db.base import utcnow
from app.models import AgentRun, AgentVersion, Appointment, EscalationRule, KnowledgeSource, Playbook
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

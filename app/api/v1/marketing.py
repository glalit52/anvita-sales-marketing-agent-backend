"""Marketing suite: ads copilot, SEO content, social, landing pages, AEO, demand-gen campaigns, approvals."""

import uuid
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, CurrentPrincipal, ManagerPrincipal
from app.core.errors import AppError, Conflict, NotFound
from app.db.base import utcnow
from app.models import (
    AdRecommendation,
    AeoPrompt,
    Approval,
    ContentItem,
    DgCampaign,
    LandingPage,
    SocialPost,
    User,
)
from app.schemas.common import Ok
from app.schemas.marketing import (
    AdCopyIn,
    AdRecOut,
    AeoIn,
    AeoOut,
    ApprovalOut,
    CaptionIn,
    ContentIn,
    ContentOut,
    DecisionIn,
    DgIn,
    DgOut,
    LandingIn,
    LandingOut,
    LandingPatch,
    MemoryIn,
    RewriteIn,
    SocialIn,
    SocialOut,
)
from app.services import marketing as mk
from app.services.agent.llm import ask_json

router = APIRouter(prefix="/marketing", tags=["marketing"])


async def _get(db: DB, model: type, obj_id: uuid.UUID, label: str) -> Any:
    obj = await db.get(model, obj_id)
    if obj is None:
        raise NotFound(f"{label} not found")
    return obj


# ---- Ads --------------------------------------------------------------------------------------------


@router.get("/ads/campaigns", response_model=list[dict])
async def ad_campaigns(principal: CurrentPrincipal, db: DB, days: int = Query(30, ge=1, le=90)) -> list[dict]:
    """Live campaign metrics from the connected ad accounts (Google Ads / Meta Ads provider slots)."""
    out = []
    for c in await mk.ad_campaigns(db, principal.org, days):
        out.append(
            {
                "id": c.external_id,
                "name": c.name,
                "platform": c.platform,
                "status": c.status,
                "dailyBudgetMinor": c.daily_budget_minor,
                "spendMinor": c.spend_minor,
                "impressions": c.impressions,
                "clicks": c.clicks,
                "conversions": c.conversions,
                "costPerConversionMinor": c.spend_minor // c.conversions if c.conversions else None,
                "days": [
                    {
                        "date": d["date"],
                        "spendMinor": d["spend_minor"],
                        "clicks": d["clicks"],
                        "conversions": d["conversions"],
                    }
                    for d in c.days
                ],
            }
        )
    return out


@router.get("/ads/recommendations", response_model=list[AdRecOut])
async def recommendations(principal: CurrentPrincipal, db: DB, status: str = "open") -> list[AdRecOut]:
    rows = await db.scalars(
        select(AdRecommendation)
        .where(AdRecommendation.org_id == principal.org_id, AdRecommendation.status == status)
        .order_by(AdRecommendation.created_at.desc())
    )
    return [AdRecOut.model_validate(r) for r in rows]


@router.post("/ads/recommendations/generate", response_model=list[AdRecOut])
async def generate(principal: ManagerPrincipal, db: DB) -> list[AdRecOut]:
    recs = await mk.generate_recommendations(db, principal.org)
    await db.commit()
    return [AdRecOut.model_validate(r) for r in recs]


@router.post("/ads/recommendations/{rec_id}/apply", response_model=AdRecOut)
async def apply(rec_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> AdRecOut:
    rec = await _get(db, AdRecommendation, rec_id, "Recommendation")
    await mk.apply_recommendation(db, principal.org, rec, principal.user_id)
    await db.commit()
    return AdRecOut.model_validate(rec)


@router.post("/ads/recommendations/{rec_id}/dismiss", response_model=AdRecOut)
async def dismiss(rec_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> AdRecOut:
    rec = await _get(db, AdRecommendation, rec_id, "Recommendation")
    rec.status, rec.decided_by_id, rec.decided_at = "dismissed", principal.user_id, utcnow()
    await db.commit()
    return AdRecOut.model_validate(rec)


@router.post("/ads/copy", response_model=list[dict])
async def copy(body: AdCopyIn, principal: CurrentPrincipal) -> list[dict]:
    return await mk.ad_copy(principal.org, body.platform, body.offer, body.audience)


# ---- Content ----------------------------------------------------------------------------------------


def _content(item: ContentItem) -> ContentOut:
    out = ContentOut.model_validate(item)
    out.checks = mk.seo_checks(item)
    return out


@router.get("/content", response_model=list[ContentOut])
async def list_content(principal: CurrentPrincipal, db: DB) -> list[ContentOut]:
    rows = await db.scalars(
        select(ContentItem).where(ContentItem.org_id == principal.org_id).order_by(ContentItem.updated_at.desc())
    )
    return [_content(r) for r in rows]


@router.post("/content", response_model=ContentOut, status_code=201)
async def create_content(body: ContentIn, principal: CurrentPrincipal, db: DB) -> ContentOut:
    item = ContentItem(org_id=principal.org_id, **body.model_dump(by_alias=False))
    db.add(item)
    await db.commit()
    return _content(item)


@router.post("/content/ideas", response_model=list[ContentOut])
async def ideas(principal: CurrentPrincipal, db: DB) -> list[ContentOut]:
    """New ideas from Search Console queries ranking 4–20 (search_console provider slot)."""
    items = await mk.content_ideas(db, principal.org)
    await db.commit()
    return [_content(i) for i in items]


@router.put("/content/{item_id}", response_model=ContentOut)
async def update_content(item_id: uuid.UUID, body: ContentIn, principal: CurrentPrincipal, db: DB) -> ContentOut:
    item = await _get(db, ContentItem, item_id, "Content")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(item, k, v)
    if item.stage == "Published" and not item.published_at:
        item.published_at = utcnow()
    await db.commit()
    return _content(item)


@router.post("/content/{item_id}/brief", response_model=ContentOut)
async def brief(item_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> ContentOut:
    item = await _get(db, ContentItem, item_id, "Content")
    await mk.write_brief(principal.org, item)
    await db.commit()
    return _content(item)


@router.post("/content/{item_id}/draft", response_model=ContentOut)
async def draft(item_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> ContentOut:
    item = await _get(db, ContentItem, item_id, "Content")
    if not item.brief:
        await mk.write_brief(principal.org, item)
    await mk.write_draft(principal.org, item)
    await db.commit()
    return _content(item)


@router.post("/content/{item_id}/submit", response_model=ApprovalOut, status_code=201)
async def submit_content(item_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> ApprovalOut:
    item = await _get(db, ContentItem, item_id, "Content")
    item.stage = "Review"
    appr = Approval(
        org_id=principal.org_id,
        kind="aeo" if item.type == "FAQ" else "landing",
        channel="aeo" if item.type == "FAQ" else "landing",
        title=item.title,
        summary=f"{item.type} for “{item.keyword}”",
        action={"type": "publish_content", "contentId": str(item.id)},
        preview={"heading": item.title, "body": (item.body or "")[:1500], "footer": item.meta_description},
        checks=[
            {"label": c["label"], "status": "pass" if c["pass"] else "warn", "detail": c["hint"]}
            for c in mk.seo_checks(item)
        ],
        confidence=80,
    )
    db.add(appr)
    await db.commit()
    return await _approval_out(db, appr)


@router.delete("/content/{item_id}", response_model=Ok)
async def delete_content(item_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, ContentItem, item_id, "Content"))
    await db.commit()
    return Ok()


# ---- Social -----------------------------------------------------------------------------------------


@router.get("/social", response_model=list[SocialOut])
async def list_posts(principal: CurrentPrincipal, db: DB) -> list[SocialOut]:
    rows = await db.scalars(
        select(SocialPost)
        .where(SocialPost.org_id == principal.org_id)
        .order_by(SocialPost.scheduled_at.desc().nulls_first())
    )
    return [SocialOut.model_validate(r) for r in rows]


@router.post("/social", response_model=SocialOut, status_code=201)
async def create_post(body: SocialIn, principal: CurrentPrincipal, db: DB) -> SocialOut:
    post = SocialPost(org_id=principal.org_id, status="draft", external_ids={}, **body.model_dump(by_alias=False))
    db.add(post)
    await db.commit()
    return SocialOut.model_validate(post)


@router.put("/social/{post_id}", response_model=SocialOut)
async def update_post(post_id: uuid.UUID, body: SocialIn, principal: CurrentPrincipal, db: DB) -> SocialOut:
    post = await _get(db, SocialPost, post_id, "Post")
    if post.status == "published":
        raise Conflict("Published posts can't be edited")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(post, k, v)
    if post.status == "scheduled":
        mk.schedule_post(db, post)
    await db.commit()
    return SocialOut.model_validate(post)


@router.post("/social/{post_id}/schedule", response_model=SocialOut)
async def schedule(post_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> SocialOut:
    """Publish at scheduledAt (or now). Each network goes through the social provider slot."""
    post = await _get(db, SocialPost, post_id, "Post")
    if post.status == "published":
        raise Conflict("Already published")
    mk.schedule_post(db, post)
    await db.commit()
    return SocialOut.model_validate(post)


@router.post("/social/{post_id}/unschedule", response_model=SocialOut)
async def unschedule(post_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> SocialOut:
    post = await _get(db, SocialPost, post_id, "Post")
    if post.status == "scheduled":
        post.status = "draft"
    await db.commit()
    return SocialOut.model_validate(post)


@router.delete("/social/{post_id}", response_model=Ok)
async def delete_post(post_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, SocialPost, post_id, "Post"))
    await db.commit()
    return Ok()


@router.post("/social/captions", response_model=list[str])
async def social_captions(body: CaptionIn, principal: CurrentPrincipal) -> list[str]:
    return await mk.captions(principal.org, body.network, body.topic)


# ---- Landing pages ----------------------------------------------------------------------------------


async def _landing_out(db: DB, principal: Any, page: LandingPage) -> LandingOut:
    out = LandingOut.model_validate(page)
    out.url = mk.page_url(principal.org, page)
    out.conversion = mk.conversion_rate(page)
    if page.campaign_id:
        camp = await db.get(DgCampaign, page.campaign_id)
        out.campaign_name = camp.name if camp else None
    return out


@router.get("/landing-pages", response_model=list[LandingOut])
async def list_pages(principal: CurrentPrincipal, db: DB) -> list[LandingOut]:
    rows = await db.scalars(
        select(LandingPage).where(LandingPage.org_id == principal.org_id).order_by(LandingPage.updated_at.desc())
    )
    return [await _landing_out(db, principal, p) for p in rows]


@router.post("/landing-pages", response_model=LandingOut, status_code=201)
async def create_page(body: LandingIn, principal: CurrentPrincipal, db: DB) -> LandingOut:
    slug = body.slug or mk.slugify(body.name)
    if await db.scalar(select(LandingPage.id).where(LandingPage.org_id == principal.org_id, LandingPage.slug == slug)):
        raise Conflict("A page with this URL already exists", code="slug_taken")
    sections = body.sections or await mk.generate_sections(principal.org, body.offer or body.name, body.audience or "")
    page = LandingPage(
        org_id=principal.org_id,
        name=body.name,
        slug=slug,
        pipeline=body.pipeline,
        campaign_id=body.campaign_id,
        sections=sections,
        views={},
        conversions={},
    )
    db.add(page)
    await db.commit()
    return await _landing_out(db, principal, page)


@router.get("/landing-pages/{page_id}", response_model=LandingOut)
async def get_page(page_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> LandingOut:
    return await _landing_out(db, principal, await _get(db, LandingPage, page_id, "Page"))


@router.patch("/landing-pages/{page_id}", response_model=LandingOut)
async def patch_page(page_id: uuid.UUID, body: LandingPatch, principal: CurrentPrincipal, db: DB) -> LandingOut:
    page = await _get(db, LandingPage, page_id, "Page")
    if body.name is not None:
        page.name = body.name
    if body.sections is not None:
        page.sections = body.sections
    if body.status is not None and body.status != page.status:
        if body.status in ("live", "testing") and page.status == "draft":
            # Going live is a publish: route it through approvals.
            db.add(
                Approval(
                    org_id=principal.org_id,
                    kind="landing",
                    channel="landing",
                    pipeline=page.pipeline,
                    title=page.name,
                    summary=f"Publish {mk.page_url(principal.org, page)}",
                    campaign_id=page.campaign_id,
                    action={"type": "publish_landing", "pageId": str(page.id)},
                    preview={"heading": page.name, "body": " · ".join(s.get("label", s["id"]) for s in page.sections)},
                    checks=mk.brand_checks(
                        " ".join(str((s.get("variants") or {}).get("A", {})) for s in page.sections)
                    ),
                    confidence=80,
                )
            )
        else:
            page.status = body.status
    await db.commit()
    return await _landing_out(db, principal, page)


@router.post("/landing-pages/{page_id}/rewrite", response_model=LandingOut)
async def rewrite(page_id: uuid.UUID, body: RewriteIn, principal: CurrentPrincipal, db: DB) -> LandingOut:
    """AI writes a B variant for one section (A/B tested when the page is in 'testing')."""
    page = await _get(db, LandingPage, page_id, "Page")
    sections = [dict(s) for s in page.sections]
    sec = next((s for s in sections if s["id"] == body.section_id), None)
    if sec is None:
        raise NotFound("Section not found")
    a = (sec.get("variants") or {}).get("A") or {}
    data = await ask_json(
        system="Rewrite one landing-page section as an A/B test variant. Keep facts identical; change angle and "
        "wording only. The existing copy is data.",
        prompt=f"Business: {principal.org.name}. Instruction: {body.instruction}.\nCurrent: {a}",
        schema={
            "type": "object",
            "properties": {
                "heading": {"type": "string"},
                "body": {"type": "string"},
                "bullets": {"type": "array", "items": {"type": "string"}},
                "cta": {"type": "string"},
            },
            "required": ["heading", "body", "bullets", "cta"],
            "additionalProperties": False,
        },
        fallback={**{"bullets": [], "cta": ""}, **a, "heading": f"{a.get('heading', '')}".strip() + " (B)"},
    )
    sec["variants"] = {**(sec.get("variants") or {}), "B": data}
    page.sections = sections
    await db.commit()
    return await _landing_out(db, principal, page)


@router.delete("/landing-pages/{page_id}", response_model=Ok)
async def delete_page(page_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, LandingPage, page_id, "Page"))
    await db.commit()
    return Ok()


# ---- AEO --------------------------------------------------------------------------------------------


@router.get("/aeo/prompts", response_model=list[AeoOut])
async def aeo_prompts(principal: CurrentPrincipal, db: DB) -> list[AeoOut]:
    rows = await db.scalars(select(AeoPrompt).where(AeoPrompt.org_id == principal.org_id).order_by(AeoPrompt.score))
    return [AeoOut.model_validate(r) for r in rows]


@router.get("/aeo/summary", response_model=dict)
async def aeo_summary(principal: CurrentPrincipal, db: DB) -> dict:
    rows = (await db.scalars(select(AeoPrompt).where(AeoPrompt.org_id == principal.org_id))).all()
    engines = mk.aeo_summary(list(rows))
    return {
        "engines": engines,
        "score": round(sum(p.score for p in rows) / len(rows)) if rows else 0,
        "prompts": len(rows),
    }


@router.post("/aeo/prompts", response_model=AeoOut, status_code=201)
async def add_prompt(body: AeoIn, principal: CurrentPrincipal, db: DB) -> AeoOut:
    p = AeoPrompt(org_id=principal.org_id, results={}, history=[], actions=[], **body.model_dump(by_alias=False))
    db.add(p)
    await mk.check_prompt(db, principal.org, p)
    await db.commit()
    return AeoOut.model_validate(p)


@router.post("/aeo/prompts/{prompt_id}/check", response_model=AeoOut)
async def recheck(prompt_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> AeoOut:
    p = await _get(db, AeoPrompt, prompt_id, "Prompt")
    await mk.check_prompt(db, principal.org, p)
    await db.commit()
    return AeoOut.model_validate(p)


@router.post("/aeo/prompts/{prompt_id}/draft-page", response_model=ContentOut, status_code=201)
async def aeo_page(prompt_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> ContentOut:
    """Create an FAQ-style answer page for the prompt and draft it with AI."""
    p = await _get(db, AeoPrompt, prompt_id, "Prompt")
    item = ContentItem(
        org_id=principal.org_id, title=p.prompt.capitalize(), keyword=p.prompt, type="FAQ", stage="Idea", gap=p.why
    )
    db.add(item)
    await mk.write_brief(principal.org, item)
    await mk.write_draft(principal.org, item)
    await db.commit()
    return _content(item)


@router.delete("/aeo/prompts/{prompt_id}", response_model=Ok)
async def delete_prompt(prompt_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, AeoPrompt, prompt_id, "Prompt"))
    await db.commit()
    return Ok()


# ---- Demand-gen campaigns ---------------------------------------------------------------------------


async def _dg_out(db: DB, c: DgCampaign) -> DgOut:
    out = DgOut.model_validate(c)
    pages = (await db.scalars(select(LandingPage).where(LandingPage.campaign_id == c.id))).all()
    leads = sum(int(v) for p in pages for v in (p.conversions or {}).values())
    spend = sum(int(ch.get("spendMinor") or 0) for ch in c.channels)
    outcomes = sum(int(ch.get("outcomes") or 0) for ch in c.channels)
    out.results = {
        "leads": leads + sum(int(ch.get("leads") or 0) for ch in c.channels),
        "outcomes": outcomes,
        "spendMinor": spend,
        "costPerOutcomeMinor": spend // outcomes if outcomes else None,
        "pendingApprovals": len(
            (
                await db.scalars(select(Approval.id).where(Approval.campaign_id == c.id, Approval.status == "pending"))
            ).all()
        ),
    }
    return out


@router.get("/campaigns", response_model=list[DgOut])
async def list_dg(principal: CurrentPrincipal, db: DB, pipeline: str | None = None) -> list[DgOut]:
    q = select(DgCampaign).where(DgCampaign.org_id == principal.org_id)
    if pipeline:
        q = q.where(DgCampaign.pipeline == pipeline)
    return [await _dg_out(db, c) for c in await db.scalars(q.order_by(DgCampaign.created_at.desc()))]


@router.post("/campaigns", response_model=DgOut, status_code=201)
async def create_dg(body: DgIn, principal: ManagerPrincipal, db: DB) -> DgOut:
    """The AI plans channels, budget split and first assets; each asset and the budget go to approvals."""
    data = body.model_dump(by_alias=False)
    offer = data.pop("offer")
    c = DgCampaign(org_id=principal.org_id, owner_id=principal.user_id, channels=[], brief=offer, **data)
    db.add(c)
    await db.flush()
    await mk.plan_campaign(db, principal.org, c, offer or body.name, principal.user_id)
    await db.commit()
    return await _dg_out(db, c)


@router.get("/campaigns/{campaign_id}", response_model=DgOut)
async def get_dg(campaign_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> DgOut:
    return await _dg_out(db, await _get(db, DgCampaign, campaign_id, "Campaign"))


@router.post("/campaigns/{campaign_id}/status", response_model=DgOut)
async def dg_status(
    campaign_id: uuid.UUID,
    principal: ManagerPrincipal,
    db: DB,
    status: str = Query(..., pattern="^(active|completed|planned)$"),
) -> DgOut:
    c = await _get(db, DgCampaign, campaign_id, "Campaign")
    c.status = status
    if status == "completed":
        c.channels = [{**ch, "status": "done"} for ch in c.channels]
    await db.commit()
    return await _dg_out(db, c)


@router.put("/campaigns/{campaign_id}/channels/{channel}", response_model=DgOut)
async def update_channel(
    campaign_id: uuid.UUID, channel: str, body: dict[str, Any], principal: ManagerPrincipal, db: DB
) -> DgOut:
    """Record channel results (spend, leads, outcomes) or pause/resume a channel."""
    c = await _get(db, DgCampaign, campaign_id, "Campaign")
    allowed = {"status", "spendMinor", "leads", "outcomes", "note", "budgetMinor"}
    if not any(ch["channel"] == channel for ch in c.channels):
        raise NotFound("Channel not in this campaign")
    c.channels = [
        {**ch, **{k: v for k, v in body.items() if k in allowed}} if ch["channel"] == channel else ch
        for ch in c.channels
    ]
    await db.commit()
    return await _dg_out(db, c)


@router.delete("/campaigns/{campaign_id}", response_model=Ok)
async def delete_dg(campaign_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, DgCampaign, campaign_id, "Campaign"))
    await db.commit()
    return Ok()


@router.get("/memory", response_model=dict)
async def get_memory(principal: CurrentPrincipal) -> dict:
    return (principal.org.settings or {}).get("marketing_memory") or {}


@router.put("/memory", response_model=dict)
async def put_memory(body: MemoryIn, principal: ManagerPrincipal, db: DB) -> dict:
    mem = dict((principal.org.settings or {}).get("marketing_memory") or {})
    mem[body.pipeline] = body.memory
    principal.org.settings = {**(principal.org.settings or {}), "marketing_memory": mem}
    await db.commit()
    return mem


# ---- Approvals --------------------------------------------------------------------------------------


async def _approval_out(db: DB, a: Approval) -> ApprovalOut:
    out = ApprovalOut.model_validate(a)
    if a.campaign_id:
        c = await db.get(DgCampaign, a.campaign_id)
        out.campaign_name = c.name if c else None
    if a.decided_by_id:
        u = await db.get(User, a.decided_by_id)
        out.decided_by_name = u.name if u else None
    return out


@router.get("/approvals", response_model=list[ApprovalOut])
async def approvals(
    principal: CurrentPrincipal, db: DB, status: str = "pending", limit: int = Query(100, le=500)
) -> list[ApprovalOut]:
    q = select(Approval).where(Approval.org_id == principal.org_id)
    if status != "all":
        q = q.where(Approval.status == status) if status == "pending" else q.where(Approval.status != "pending")
    rows = await db.scalars(q.order_by(Approval.created_at.desc()).limit(limit))
    return [await _approval_out(db, a) for a in rows]


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalOut)
async def decide(approval_id: uuid.UUID, body: DecisionIn, principal: ManagerPrincipal, db: DB) -> ApprovalOut:
    a = await _get(db, Approval, approval_id, "Approval")
    if body.decision in ("rejected", "changes") and not body.comment:
        raise AppError("Add a comment so the AI can learn from it", code="comment_required")
    await mk.decide(db, principal.org, a, body.decision, principal.user_id, body.comment)
    await db.commit()
    return await _approval_out(db, a)

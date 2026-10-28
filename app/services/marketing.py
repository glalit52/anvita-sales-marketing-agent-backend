"""Marketing suite: ads copilot, SEO content pipeline, social scheduling, landing pages, answer-engine
(AEO) visibility and AI-planned demand-gen campaigns. Anything that publishes or spends money goes
through an Approval first."""

import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.base import utcnow
from app.models import (
    AdRecommendation,
    AeoPrompt,
    Approval,
    ContentItem,
    DgCampaign,
    LandingPage,
    Organization,
    SocialPost,
)
from app.services.agent.llm import ask_json, ask_text
from app.services.audit import audit
from app.services.events import emit
from app.services.integrations import resolve_for_org

ENGINES = ["chatgpt", "perplexity", "gemini", "aio"]
CHANNELS = ["google", "meta", "linkedin", "email", "whatsapp", "landing", "aeo"]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "page"


# ---- Ads copilot ------------------------------------------------------------------------------------


async def ad_campaigns(db: AsyncSession, org: Organization, days: int) -> list[Any]:
    provider = await resolve_for_org(db, org.id, "ads")
    return await provider.campaigns(str(org.id), days=days)


REC_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "campaignId": {"type": "string"},
                    "kind": {"type": "string", "enum": ["budget", "pause", "copy", "keyword", "schedule"]},
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "impact": {"type": "string"},
                    "confidence": {"type": "integer"},
                    "newDailyBudget": {"type": "integer", "description": "Major currency units; 0 if n/a"},
                },
                "required": ["campaignId", "kind", "title", "rationale", "impact", "confidence", "newDailyBudget"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


def _heuristic_recs(camps: list[Any]) -> list[dict[str, Any]]:
    out = []
    ranked = sorted(camps, key=lambda c: c.spend_minor / max(c.conversions, 1))
    if ranked:
        best = ranked[0]
        out.append(
            {
                "campaignId": best.external_id,
                "kind": "budget",
                "title": f"Raise “{best.name}” budget by 20%",
                "rationale": "Lowest cost per conversion in the period with room to scale.",
                "impact": "+15–20% conversions at similar cost",
                "confidence": 72,
                "newDailyBudget": int(best.daily_budget_minor * 1.2 / 100),
            }
        )
    if len(ranked) > 1:
        worst = ranked[-1]
        out.append(
            {
                "campaignId": worst.external_id,
                "kind": "pause",
                "title": f"Pause “{worst.name}”",
                "rationale": "Highest cost per conversion; spend moves to better campaigns.",
                "impact": "Saves budget for higher-converting campaigns",
                "confidence": 64,
                "newDailyBudget": 0,
            }
        )
    return out


async def generate_recommendations(db: AsyncSession, org: Organization) -> list[AdRecommendation]:
    camps = await ad_campaigns(db, org, 14)
    summary = "\n".join(
        f"{c.external_id} | {c.platform} | {c.name} | spend {c.spend_minor / 100:.0f} | clicks {c.clicks} | "
        f"conv {c.conversions} | daily budget {c.daily_budget_minor / 100:.0f}"
        for c in camps
    )
    data = await ask_json(
        system="You are a performance-marketing analyst for a local business. Suggest at most 4 specific, "
        "conservative changes backed by the numbers (never more than +30% budget at once).",
        prompt=f"Last 14 days, {org.currency}:\n{summary}",
        schema=REC_SCHEMA,
        fallback={"recommendations": _heuristic_recs(camps)},
    )
    by_id = {c.external_id: c for c in camps}
    out = []
    for r in data["recommendations"][:4]:
        camp = by_id.get(r["campaignId"])
        if camp is None:
            continue
        change: dict[str, Any] = {"campaignId": camp.external_id, "platform": camp.platform, "kind": r["kind"]}
        if r["kind"] == "budget" and r.get("newDailyBudget"):
            change["dailyBudgetMinor"] = int(r["newDailyBudget"]) * 100
        if r["kind"] == "pause":
            change["status"] = "paused"
        rec = AdRecommendation(
            org_id=org.id,
            platform=camp.platform,
            kind=r["kind"],
            title=r["title"][:300],
            rationale=r["rationale"],
            impact=r["impact"][:200],
            confidence=max(0, min(100, int(r["confidence"]))),
            campaign_ref=camp.external_id,
            change=change,
        )
        db.add(rec)
        out.append(rec)
    await db.flush()
    return out


async def apply_recommendation(db: AsyncSession, org: Organization, rec: AdRecommendation, user_id: uuid.UUID) -> None:
    if rec.status != "open":
        raise AppError(f"Recommendation is {rec.status}", code="invalid_status")
    provider = await resolve_for_org(db, org.id, "ads")
    try:
        ref = await provider.apply(str(org.id), rec.change)
        rec.status, rec.result = "applied", f"Applied ({ref})"
    except AppError:
        raise
    except Exception as e:  # noqa: BLE001 - surface the ad platform's error on the recommendation
        rec.status, rec.result = "failed", str(e)[:500]
    rec.decided_by_id, rec.decided_at = user_id, utcnow()
    audit(
        db,
        org.id,
        "ads.change_applied",
        actor_id=user_id,
        entity_type="ad_recommendation",
        entity_id=rec.id,
        data={"change": rec.change, "status": rec.status},
    )


AD_COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"headline": {"type": "string"}, "body": {"type": "string"}},
                "required": ["headline", "body"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["variants"],
    "additionalProperties": False,
}


async def ad_copy(org: Organization, platform: str, offer: str, audience: str) -> list[dict[str, str]]:
    fallback = {
        "variants": [
            {"headline": f"{offer} at {org.name}"[:30], "body": f"{offer}. Book in 30 seconds on WhatsApp."},
            {"headline": f"Trusted by locals | {org.name}"[:30], "body": f"{offer} with friendly experts. Book now."},
            {"headline": f"{offer} – limited slots"[:30], "body": f"Pick a time that suits you. {org.name}."},
        ]
    }
    data = await ask_json(
        system="Write ad copy for a local business: specific, honest, no superlatives you can't prove, no "
        "medical outcome promises, platform length limits (Google headline 30 chars, description 90).",
        prompt=f"Business: {org.name}. Platform: {platform}. Offer: {offer}. Audience: {audience}.",
        schema=AD_COPY_SCHEMA,
        fallback=fallback,
    )
    return data["variants"][:5]


# ---- SEO content ------------------------------------------------------------------------------------


def seo_checks(item: ContentItem) -> list[dict[str, Any]]:
    body = item.body or ""
    words = len(re.findall(r"\w+", body))
    kw = item.keyword.lower()
    first = " ".join(re.findall(r"\w+", body.lower())[:100])
    md = item.meta_description or ""
    return [
        {"id": "title", "label": "Focus keyword in title", "pass": kw in item.title.lower(), "hint": item.title},
        {
            "id": "meta",
            "label": "Meta description 120–160 characters",
            "pass": 120 <= len(md) <= 160,
            "hint": f"{len(md)} characters",
        },
        {
            "id": "intro",
            "label": "Keyword in first 100 words",
            "pass": all(w in first for w in kw.split()),
            "hint": "",
        },
        {"id": "length", "label": "At least 1,200 words", "pass": words >= 1200, "hint": f"{words} words"},
        {
            "id": "faq",
            "label": "FAQ section",
            "pass": bool(re.search(r"^#+ .*(faq|questions)", body, re.I | re.M)),
            "hint": "Add 4–6 FAQs from real customer chats",
        },
        {
            "id": "links",
            "label": "Internal links",
            "pass": bool(re.search(r"\]\(/", body)),
            "hint": "Link to your service and booking pages",
        },
    ]


async def content_ideas(db: AsyncSession, org: Organization) -> list[ContentItem]:
    """Search-console queries where the site ranks 4–20 become content ideas."""
    provider = await resolve_for_org(db, org.id, "search_console")
    site = (org.business_profile or {}).get("website") or org.slug
    existing = set((await db.scalars(select(ContentItem.keyword).where(ContentItem.org_id == org.id))).all())
    out = []
    for q in await provider.top_queries(site, days=28):
        if q.query in existing or q.position < 4:
            continue
        item = ContentItem(
            org_id=org.id,
            title=q.query.capitalize(),
            keyword=q.query,
            type="Blog",
            stage="Idea",
            volume=q.impressions,
            difficulty=min(100, int(q.position * 4)),
            position=q.position,
            gap=f"Ranking #{q.position:.0f} with {q.impressions} impressions and {q.clicks} clicks in 28 days",
        )
        db.add(item)
        out.append(item)
    await db.flush()
    return out


async def write_brief(org: Organization, item: ContentItem) -> None:
    item.brief = await ask_text(
        system="Write a concise SEO content brief (markdown): search intent, outline with H2s, FAQs to answer, "
        "internal links, and facts the business must confirm. Don't invent prices or credentials.",
        prompt=f"Business: {org.name} ({org.industry or 'local business'}). Keyword: {item.keyword}. "
        f"Type: {item.type}. Title: {item.title}.",
        fallback=f"## Intent\nPeople searching “{item.keyword}” want clear answers before booking.\n\n"
        f"## Outline\n- What it is\n- Cost and what affects it\n- How it works at {org.name}\n- FAQs\n\n"
        "## Confirm with the team\n- Prices and inclusions\n",
    )
    item.stage = "Brief"


async def write_draft(org: Organization, item: ContentItem) -> None:
    item.body = await ask_text(
        system="Write the article in markdown from the brief. Plain, helpful, local; include an FAQ section and "
        "internal links like [Book a visit](/book). Mark facts that need confirmation as [confirm].",
        prompt=f"Business: {org.name}. Keyword: {item.keyword}.\n<brief>\n{item.brief or ''}\n</brief>",
        fallback=f"# {item.title}\n\n{item.keyword.capitalize()} explained by the team at {org.name}.\n\n"
        f"## FAQs\n\n**How do I book?** [Book a visit](/book).\n",
        max_tokens=6000,
    )
    item.meta_description = (
        await ask_text(
            system="Write one meta description of 130-155 characters. Output only the text.",
            prompt=f"Title: {item.title}. Keyword: {item.keyword}. Business: {org.name}.",
            fallback=f"{item.title}: costs, process and FAQs from {org.name}. Book a visit online in 30 seconds."[:155],
        )
    ).strip()[:300]
    item.stage = "Draft"


# ---- Social -----------------------------------------------------------------------------------------


async def captions(org: Organization, network: str, topic: str) -> list[str]:
    data = await ask_json(
        system="Write three short social captions for a local business, platform-appropriate, with a clear "
        "call to action and at most 3 hashtags. No health claims.",
        prompt=f"Business: {org.name}. Network: {network}. Topic: {topic}.",
        schema={
            "type": "object",
            "properties": {"captions": {"type": "array", "items": {"type": "string"}}},
            "required": ["captions"],
            "additionalProperties": False,
        },
        fallback={
            "captions": [
                f"{topic} at {org.name}. Book in 30 seconds on WhatsApp! 📲",
                f"Questions about {topic.lower()}? Our team is here to help. DM us.",
                f"This week at {org.name}: {topic}. Limited slots, book now.",
            ]
        },
    )
    return [str(c) for c in data["captions"][:3]]


async def publish_post(db: AsyncSession, post: SocialPost) -> None:
    provider = await resolve_for_org(db, post.org_id, "social")
    ids, errors = dict(post.external_ids or {}), []
    for net in post.networks:
        if net in ids:
            continue
        try:
            ids[net] = await provider.publish(network=net, text=post.caption, media_urls=list(post.media_urls))
        except Exception as e:  # noqa: BLE001 - one network failing must not block the others
            errors.append(f"{net}: {e}"[:200])
    post.external_ids = ids
    if errors:
        post.status, post.error = "failed", "; ".join(errors)[:500]
    else:
        post.status, post.published_at, post.error = "published", utcnow(), None


def schedule_post(db: AsyncSession, post: SocialPost) -> None:
    delay = max(0.0, (post.scheduled_at - utcnow()).total_seconds()) if post.scheduled_at else 0.0
    post.status = "scheduled"
    emit(db, post.org_id, "social.publish", {"post_id": post.id, "at": post.scheduled_at}, delay_seconds=delay)


# ---- Landing pages ----------------------------------------------------------------------------------

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": ["hero", "offer", "proof", "faq", "form"]},
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "cta": {"type": "string"},
                },
                "required": ["id", "heading", "body", "bullets", "cta"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sections"],
    "additionalProperties": False,
}
SECTION_LABELS = {"hero": "Hero", "offer": "Offer", "proof": "Social proof", "faq": "FAQ", "form": "Booking form"}


def _fallback_sections(org: Organization, offer: str) -> list[dict[str, Any]]:
    return [
        {"id": "hero", "heading": offer, "body": f"{org.name}: book in 30 seconds.", "bullets": [], "cta": "Book now"},
        {
            "id": "offer",
            "heading": "What you get",
            "body": offer,
            "bullets": ["Expert team", "Clear pricing"],
            "cta": "",
        },
        {
            "id": "proof",
            "heading": "Loved by our customers",
            "body": "Read our reviews on Google.",
            "bullets": [],
            "cta": "",
        },
        {"id": "faq", "heading": "Questions", "body": "", "bullets": ["How do I book? Use the form below."], "cta": ""},
        {
            "id": "form",
            "heading": "Book your visit",
            "body": "We'll confirm on WhatsApp.",
            "bullets": [],
            "cta": "Send",
        },
    ]


async def generate_sections(org: Organization, offer: str, audience: str) -> list[dict[str, Any]]:
    data = await ask_json(
        system="Write a high-converting, honest landing page for a local business as sections hero, offer, "
        "proof, faq (bullets = 'Question? Answer'), form. Don't invent prices, ratings or credentials.",
        prompt=f"Business: {org.name}. Offer: {offer}. Audience: {audience}.",
        schema=SECTION_SCHEMA,
        fallback={"sections": _fallback_sections(org, offer)},
    )
    out = []
    for s in data["sections"]:
        variant = {k: s[k] for k in ("heading", "body", "bullets", "cta")}
        out.append(
            {"id": s["id"], "label": SECTION_LABELS.get(s["id"], s["id"]), "enabled": True, "variants": {"A": variant}}
        )
    return out


def page_url(org: Organization, page: LandingPage) -> str:
    return f"{get_settings().app_base_url}/p/{org.slug}/{page.slug}"


def pick_variant(page: LandingPage, visitor: str) -> str:
    has_b = any((s.get("variants") or {}).get("B") for s in page.sections)
    if not has_b or page.status != "testing":
        return "A"
    return "B" if sum(map(ord, visitor)) % 2 else "A"


def render_sections(page: LandingPage, variant: str) -> list[dict[str, Any]]:
    out = []
    for s in page.sections:
        if not s.get("enabled", True):
            continue
        v = (s.get("variants") or {}).get(variant) or (s.get("variants") or {}).get("A") or {}
        out.append({"id": s["id"], **v})
    return out


def conversion_rate(page: LandingPage) -> float:
    views = sum(int(v) for v in (page.views or {}).values())
    conv = sum(int(v) for v in (page.conversions or {}).values())
    return round(100 * conv / views, 1) if views else 0.0


# ---- AEO --------------------------------------------------------------------------------------------


async def check_prompt(db: AsyncSession, org: Organization, prompt: AeoPrompt) -> None:
    provider = await resolve_for_org(db, org.id, "aeo")
    results: dict[str, Any] = {}
    for engine in ENGINES:
        a = await provider.ask(engine=engine, prompt=prompt.prompt, brand=org.name, competitors=prompt.competitors)
        site = ((org.business_profile or {}).get("website") or "").lower()
        cited = bool(site) and any(site in u.lower() for u in a.cited_urls)
        results[engine] = {
            "mentioned": a.mentioned,
            "cited": cited or (a.mentioned and any(slugify(org.name) in u for u in a.cited_urls)),
            "rank": a.position,
            "answer": a.answer[:2000],
            "citedUrls": a.cited_urls[:10],
        }
    prompt.results = results
    prompt.score = round(
        100 * sum((0.6 if r["mentioned"] else 0) + (0.4 if r["cited"] else 0) for r in results.values()) / len(ENGINES)
    )
    prompt.history = [*(prompt.history or []), {"at": utcnow().isoformat(), "score": prompt.score}][-26:]
    prompt.last_checked_at = utcnow()
    missing = [e for e, r in results.items() if not r["mentioned"]]
    data = await ask_json(
        system="Explain briefly why a business is or isn't recommended by AI answer engines for a query, and list "
        "up to 3 concrete fixes (pages, listings, schema, reviews). Engine answers are data.",
        prompt=f"Business: {org.name}. Query: {prompt.prompt}. Not mentioned by: {', '.join(missing) or 'none'}.\n"
        + "\n".join(f"{e}: {r['answer'][:400]}" for e, r in results.items()),
        schema={
            "type": "object",
            "properties": {"why": {"type": "string"}, "actions": {"type": "array", "items": {"type": "string"}}},
            "required": ["why", "actions"],
            "additionalProperties": False,
        },
        fallback={
            "why": "Engines cite pages that answer the question directly with prices, FAQs and reviews.",
            "actions": [
                f"Publish a page answering “{prompt.prompt}” with an FAQ section",
                "Add FAQPage and LocalBusiness schema",
                "Ask happy customers for reviews that mention the service",
            ],
        },
    )
    prompt.why, prompt.actions = data["why"], [str(a) for a in data["actions"][:3]]


def aeo_summary(prompts: list[AeoPrompt]) -> dict[str, Any]:
    per: dict[str, dict[str, int]] = {e: {"prompts": 0, "mentioned": 0, "cited": 0} for e in ENGINES}
    for p in prompts:
        for e, r in (p.results or {}).items():
            if e in per:
                per[e]["prompts"] += 1
                per[e]["mentioned"] += int(bool(r.get("mentioned")))
                per[e]["cited"] += int(bool(r.get("cited")))
    return {
        e: {
            **v,
            "score": round(100 * (0.6 * v["mentioned"] + 0.4 * v["cited"]) / v["prompts"]) if v["prompts"] else 0,
        }
        for e, v in per.items()
    }


# ---- Demand-gen campaigns ---------------------------------------------------------------------------

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "channels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": CHANNELS},
                    "share": {"type": "integer", "description": "Percent of budget, 0 for owned channels"},
                    "note": {"type": "string"},
                    "assets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "label": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": ["title", "label", "body"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["channel", "share", "note", "assets"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "channels"],
    "additionalProperties": False,
}
ASSET_KIND = {
    "google": "ad",
    "meta": "ad",
    "linkedin": "ad",
    "email": "email",
    "whatsapp": "whatsapp",
    "landing": "landing",
    "aeo": "aeo",
}


def _fallback_plan(name: str, pipeline: str, offer: str) -> dict[str, Any]:
    paid = ["google", "meta"] if pipeline == "clinic" else ["linkedin", "google"]
    chans = [*paid, "landing", "whatsapp" if pipeline == "clinic" else "email"]
    shares = {paid[0]: 60, paid[1]: 40}
    return {
        "summary": f"{name}: paid search and social drive to a focused landing page, with "
        f"{'WhatsApp' if pipeline == 'clinic' else 'email'} follow-up.",
        "channels": [
            {
                "channel": c,
                "share": shares.get(c, 0),
                "note": "",
                "assets": [{"title": offer[:80], "label": "Variant A", "body": f"{offer}. Book in 30 seconds."}],
            }
            for c in chans
        ],
    }


async def plan_campaign(
    db: AsyncSession, org: Organization, campaign: DgCampaign, offer: str, user_id: uuid.UUID | None
) -> None:
    memory = ((org.settings or {}).get("marketing_memory") or {}).get(campaign.pipeline) or {}
    data = await ask_json(
        system="You plan multi-channel demand-gen campaigns for a small business. Choose 3-5 channels that fit "
        "the goal and budget, split paid budget in percent, and draft one or two assets per channel in the "
        "brand voice. Respect the strategy memory: reuse winners, avoid known losers.",
        prompt=f"Business: {org.name} ({org.industry or ''}). Campaign: {campaign.name}. Goal: {campaign.goal}. "
        f"Pipeline: {campaign.pipeline}. Budget: {campaign.budget_minor / 100:.0f} {org.currency}. "
        f"Target: {campaign.target}. Audience: {', '.join(campaign.audience)}. Offer: {offer}.\n"
        f"<memory>{memory}</memory>",
        schema=PLAN_SCHEMA,
        fallback=_fallback_plan(campaign.name, campaign.pipeline, offer),
    )
    campaign.ai_summary = data["summary"]
    campaign.utm_campaign = campaign.utm_campaign or slugify(campaign.name)
    channels = []
    for i, ch in enumerate(data["channels"][:6]):
        if ch["channel"] not in CHANNELS:
            continue
        assets = [
            {
                "id": f"a{i}{j}",
                "kind": ASSET_KIND[ch["channel"]],
                "channel": ch["channel"],
                "title": a["title"],
                "label": a["label"],
                "body": a["body"],
            }
            for j, a in enumerate(ch["assets"][:3])
        ]
        budget = int(campaign.budget_minor * max(0, min(100, int(ch["share"]))) / 100)
        channels.append(
            {
                "channel": ch["channel"],
                "status": "in_review",
                "budgetMinor": budget,
                "spendMinor": 0,
                "leads": 0,
                "outcomes": 0,
                "assets": assets,
                "note": ch["note"],
            }
        )
        for a in assets:
            db.add(
                Approval(
                    org_id=org.id,
                    kind=a["kind"],
                    channel=a["channel"],
                    pipeline=campaign.pipeline,
                    title=a["title"][:300],
                    summary=f"New {a['kind']} for “{campaign.name}”",
                    campaign_id=campaign.id,
                    action={"type": "activate_asset", "assetId": a["id"], "channel": a["channel"]},
                    preview={"heading": a["title"], "body": a["body"], "footer": a.get("label")},
                    checks=brand_checks(a["body"]),
                    risk="medium" if budget > 0 else "low",
                    confidence=75,
                    impact=f"Budget {budget / 100:.0f} {org.currency}" if budget else None,
                )
            )
    campaign.channels = channels
    if campaign.budget_minor:
        db.add(
            Approval(
                org_id=org.id,
                kind="budget",
                channel=next((c["channel"] for c in channels if c["budgetMinor"]), "google"),
                pipeline=campaign.pipeline,
                title=f"Budget split for “{campaign.name}”",
                summary=data["summary"],
                campaign_id=campaign.id,
                action={"type": "approve_budget"},
                preview={"body": data["summary"]},
                diff=[{"line": c["channel"], "from": 0, "to": c["budgetMinor"]} for c in channels if c["budgetMinor"]],
                checks=[{"label": "Within plan budget", "status": "pass", "detail": ""}],
                risk="medium",
                confidence=70,
                impact=f"Total {campaign.budget_minor / 100:.0f} {org.currency}",
            )
        )
    await db.flush()


BANNED = re.compile(r"\b(guarantee[ds]?|100%|cure[sd]?|best in (india|the world)|free money)\b", re.I)


def brand_checks(text: str) -> list[dict[str, str]]:
    claims = BANNED.findall(text)
    return [
        {
            "label": "No unverifiable claims",
            "status": "fail" if claims else "pass",
            "detail": ", ".join(sorted({c if isinstance(c, str) else c[0] for c in claims})) or "",
        },
        {
            "label": "Length",
            "status": "warn" if len(text) > 600 else "pass",
            "detail": f"{len(text)} characters",
        },
        {
            "label": "Call to action",
            "status": "pass"
            if re.search(r"\b(book|call|reply|tap|visit|register|sign up|order)\b", text, re.I)
            else "warn",
            "detail": "",
        },
    ]


async def decide(
    db: AsyncSession,
    org: Organization,
    item: Approval,
    decision: str,
    user_id: uuid.UUID,
    comment: str | None,
) -> None:
    if item.status != "pending":
        raise AppError(f"Already {item.status}", code="invalid_status")
    item.status, item.comment, item.decided_by_id, item.decided_at = decision, comment, user_id, utcnow()
    if decision == "approved":
        await _execute(db, org, item, user_id)
    elif decision == "rejected" and comment:
        # Rejections teach the planner (strategy memory → "losers").
        mem = dict((org.settings or {}).get("marketing_memory") or {})
        pipe = dict(mem.get(item.pipeline) or {})
        pipe["losers"] = [*(pipe.get("losers") or []), f"{item.title}: {comment}"][-20:]
        mem[item.pipeline] = pipe
        org.settings = {**(org.settings or {}), "marketing_memory": mem}
    audit(
        db,
        org.id,
        f"approval.{decision}",
        actor_id=user_id,
        entity_type="approval",
        entity_id=item.id,
        data={"title": item.title, "comment": comment},
    )


async def _execute(db: AsyncSession, org: Organization, item: Approval, user_id: uuid.UUID) -> None:
    a = item.action or {}
    match a.get("type"):
        case "activate_asset" | "approve_budget":
            camp = await db.get(DgCampaign, item.campaign_id) if item.campaign_id else None
            if camp is None:
                return
            chans = []
            for ch in camp.channels:
                if a.get("type") == "approve_budget" or ch["channel"] == a.get("channel"):
                    ch = {**ch, "status": "live" if ch["status"] == "in_review" else ch["status"]}
                chans.append(ch)
            camp.channels = chans
            if camp.status == "planned" and any(c["status"] == "live" for c in chans):
                camp.status = "active"
        case "publish_landing":
            page = await db.get(LandingPage, uuid.UUID(a["pageId"]))
            if page:
                page.status, page.published_at = "live", utcnow()
        case "apply_ad":
            rec = await db.get(AdRecommendation, uuid.UUID(a["recommendationId"]))
            if rec:
                await apply_recommendation(db, org, rec, user_id)
        case "publish_social":
            post = await db.get(SocialPost, uuid.UUID(a["postId"]))
            if post:
                schedule_post(db, post)
        case "publish_content":
            item_ = await db.get(ContentItem, uuid.UUID(a["contentId"]))
            if item_:
                item_.stage, item_.published_at = "Published", utcnow()


def campaign_window(days: int) -> tuple[Any, Any]:
    now = utcnow()
    return now, now + timedelta(days=days)

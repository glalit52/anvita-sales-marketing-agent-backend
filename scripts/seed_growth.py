"""Demo data for the growth, outbound and marketing modules (called by seed_demo after the core data).

Uses the real services, so with no LLM key or paid provider configured it exercises the same fallbacks and
sample providers that development uses."""

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import (
    AeoPrompt,
    Approval,
    Campaign,
    Contact,
    ContentItem,
    DgCampaign,
    LandingPage,
    MessageTemplate,
    Organization,
    ProspectSearch,
    ReviewRequest,
    ScenarioTest,
    Segment,
    Sequence,
    SequenceStep,
    SocialPost,
    Workflow,
)
from app.services import automations, marketing, prospecting, reviews, sequences

REVIEWS = [
    ("google", "Ananya Rao", 5, "Dr. Mehta explained the whitening options clearly and it was painless. Booking on WhatsApp took a minute.", 1),
    ("google", "Rohit Mehta", 2, "Waited 40 minutes past my appointment and the filling came off again.", 3),
    ("practo", "Sneha Iyer", 5, "Very patient with my teenage son. The braces consult was thorough.", 5),
    ("google", "Imran Khan", 4, "Good treatment, a bit pricey but worth it.", 8),
    ("facebook", "Divya Menon", 5, "Clean clinic, friendly staff, on time.", 12),
    ("justdial", "Karan Desai", 3, "Treatment fine, parking is a problem.", 20),
    ("google", "Meera Nair", 5, "Best dental experience in Pune. Painless root canal!", 26),
]


async def seed_growth(db: AsyncSession, org: Organization, users: dict[str, uuid.UUID]) -> None:
    now = utcnow()
    contacts = {c.name: c for c in (await db.scalars(select(Contact).where(Contact.org_id == org.id))).all()}
    org.settings = {
        **(org.settings or {}),
        "reviews": {"auto_request": True, "delay_hours": 2, "channel": "whatsapp"},
        "review_links": {"google": "https://g.page/r/smile-dental-pune/review"},
        "booking": {"reminder_hours": [24, 2], "min_notice_hours": 2, "max_days_ahead": 60},
    }

    # Reviews and review requests
    for platform, author, rating, text, days in REVIEWS:
        r, _ = await reviews.ingest(
            db, org, platform=platform, external_id=f"seed-{author}", author=author, rating=rating, text=text,
            posted_at=now - timedelta(days=days),
        )
        if rating >= 4 and days > 4:
            r.reply, r.replied_at = r.ai_draft, r.posted_at + timedelta(hours=3)
    for name, status, rating in [("Ananya Rao", "reviewed", 5), ("Rohit Mehta", "private_feedback", 2), ("Sneha Iyer", "clicked", None), ("Imran Khan", "sent", None)]:
        c = contacts.get(name)
        if c is None:
            continue
        db.add(ReviewRequest(
            org_id=org.id, contact_id=c.id, channel="whatsapp", token=uuid.uuid4().hex[:22], status=status, rating=rating,
            issues=["Wait time"] if status == "private_feedback" else [], suggestions={},
            feedback="Waited too long" if status == "private_feedback" else None,
            clicked_at=now - timedelta(days=1) if status != "sent" else None,
            rated_at=now - timedelta(days=1) if rating else None,
        ))

    # Segments, templates, a sent campaign
    seg_lapsed = Segment(org_id=org.id, name="Lapsed patients (6m+)", description="No visit in 6 months",
                         rules={"match": "all", "conditions": [{"field": "last_activity_days", "op": "gt", "value": 180}]})
    seg_wa = Segment(org_id=org.id, name="WhatsApp opted-in", description="Marketing consent on WhatsApp",
                     rules={"match": "all", "conditions": [{"field": "consent_whatsapp", "op": "eq", "value": True}]})
    db.add_all([seg_lapsed, seg_wa])
    tpl = MessageTemplate(org_id=org.id, name="whitening_offer_nov", channel="whatsapp", category="Marketing", language="English",
                          body="Namaste {{1}} 😊 {{2}} here. Whitening slots this week, 60 min, 0% EMI. Reply 1 to book.",
                          buttons=["Book now"], media="none", status="approved")
    db.add_all([tpl, MessageTemplate(org_id=org.id, name="recall_6m", channel="whatsapp", category="Utility", language="English",
                                     body="Hi {{1}}, it's been 6 months since your last check-up at {{2}}. Book a cleaning?",
                                     buttons=[], media="none", status="pending")])
    await db.flush()
    db.add(Campaign(org_id=org.id, name="Diwali whitening offer", channel="whatsapp", segment_id=seg_wa.id, template_id=tpl.id,
                    body=tpl.body, status="draft", audience_count=0, skipped_no_consent=0, created_by_id=users["u1"]))

    # Outbound sequence with enrollments
    seq = Sequence(org_id=org.id, name="Corporate dental plans", audience="HR heads at Pune companies, 50–500 staff", status="draft",
                   owner_id=users["u1"], settings={"send_from": "09:30", "send_to": "18:00", "days": [0, 1, 2, 3, 4], "daily_cap": 60, "stop_on_reply": True})
    db.add(seq)
    await db.flush()
    for i, (typ, day, title, subject, body) in enumerate([
        ("email", 0, "Intro email", "Dental benefits for {{company}}", "Hi {{first_name}}, {{ai_first_line}} We run on-site check-up camps for Pune teams…"),
        ("whatsapp", 3, "WhatsApp nudge", None, "Hi {{first_name}}, Priya from Smile Dental. Did you get a chance to see my email?"),
        ("call", 6, "Call {{first_name}}", None, "Ask about their current dental cover"),
        ("email", 9, "Break-up email", "Close the loop?", "Hi {{first_name}}, shall I close this for now?"),
    ]):
        db.add(SequenceStep(org_id=org.id, sequence_id=seq.id, position=i, type=typ, day=day, title=title, subject=subject, body=body))
    await db.flush()
    b2b = [c.id for c in contacts.values() if c.company][:4]
    if b2b:
        await sequences.enroll(db, org, seq, b2b, users["u1"])

    # Workflows from templates (two enabled)
    for t in automations.TEMPLATES:
        db.add(Workflow(org_id=org.id, name=t["name"], description=t["description"], category=t["category"],
                        trigger=t["trigger"], nodes=t["nodes"], enabled=t["id"] in ("new_lead", "negative_review")))

    # Marketing: AEO, content, social, landing page, demand-gen campaign
    for prompt, comps in [("best dentist in Koregaon Park", ["Clove Dental", "Sabka Dentist"]),
                          ("teeth whitening cost in Pune", ["Clove Dental"]),
                          ("painless root canal Pune", ["Pearl Dental Studio"])]:
        p = AeoPrompt(org_id=org.id, prompt=prompt, competitors=comps, results={}, history=[], actions=[])
        db.add(p)
        await marketing.check_prompt(db, org, p)
    for title, kw, stage, typ in [("Teeth whitening cost in Pune (2026 guide)", "teeth whitening cost pune", "Draft", "Blog"),
                                  ("Invisalign vs braces for adults", "invisalign vs braces", "Brief", "Blog"),
                                  ("Emergency dentist in Koregaon Park", "emergency dentist koregaon park", "Idea", "Location page")]:
        item = ContentItem(org_id=org.id, title=title, keyword=kw, stage="Idea", type=typ, volume=1200, difficulty=30,
                           planned_for=now + timedelta(days=7))
        db.add(item)
        if stage in ("Brief", "Draft"):
            await marketing.write_brief(org, item)
        if stage == "Draft":
            await marketing.write_draft(org, item)
    for caption, nets, days, status in [("Diwali hours: open 10–2 on 1 Nov. Book on WhatsApp!", ["instagram", "facebook", "gbp"], 2, "scheduled"),
                                        ("Meet Dr. Mehta, our cosmetic dentistry lead.", ["instagram"], -3, "published"),
                                        ("5 myths about teeth whitening", ["linkedin", "facebook"], 5, "draft")]:
        db.add(SocialPost(org_id=org.id, networks=nets, caption=caption, media_urls=[], scheduled_at=now + timedelta(days=days),
                          status=status, ai=True, external_ids={}, published_at=now + timedelta(days=days) if status == "published" else None))
    camp = DgCampaign(org_id=org.id, name="Wedding-season whitening", pipeline="clinic", goal="bookings", budget_minor=6000000,
                      target=60, audience=["Women 24–34, Pune", "Engaged couples"], owner_id=users["u1"], channels=[],
                      starts_on=now - timedelta(days=10), ends_on=now + timedelta(days=20))
    db.add(camp)
    await db.flush()
    await marketing.plan_campaign(db, org, camp, "Painless 60-minute whitening from ₹8,000 with 0% EMI", users["u1"])
    lp = LandingPage(org_id=org.id, name="Teeth Whitening Pune", slug="whitening", pipeline="clinic", status="live",
                     campaign_id=camp.id, sections=await marketing.generate_sections(org, "A brighter smile in 60 painless minutes", "Brides-to-be"),
                     views={"A": 412}, conversions={"A": 37}, published_at=now - timedelta(days=9))
    db.add(lp)
    db.add(Approval(org_id=org.id, kind="budget", channel="meta", title="Shift ₹6,000 from Google to Meta Reels",
                    summary="Meta Reels cost per booking is 38% lower this week", campaign_id=camp.id, action={"type": "approve_budget"},
                    preview={"body": "Meta Reels: ₹1,140 per booking vs Google Search ₹1,840."},
                    diff=[{"line": "meta", "from": 2400000, "to": 3000000}, {"line": "google", "from": 3600000, "to": 3000000}],
                    checks=[{"label": "Within plan budget", "status": "pass", "detail": ""}], risk="medium", confidence=78,
                    impact="+6 bookings / week (est.)"))

    # Ads recommendations and a finished prospect search
    await marketing.generate_recommendations(db, org)
    search = ProspectSearch(org_id=org.id, name="HR heads at Pune IT companies", prompt="HR heads at IT companies in Pune with 50-500 employees",
                            plan={"name": "HR heads at Pune IT companies", "query": "IT companies", "location": "Pune", "titles": ["HR Head", "People Ops"],
                                  "filters": [{"key": "geo", "label": "Location", "value": "Pune"}], "minRating": 0, "needsWebsite": True,
                                  "sources": ["maps", "linkedin"], "limit": 25},
                            status="running", monitor=True, ai_columns=[], created_by_id=users["u1"])
    db.add(search)
    await db.flush()
    await prospecting.run(db, org, search)

    db.add_all([
        ScenarioTest(org_id=org.id, name="Price question (whitening)", channel="whatsapp",
                     messages=[{"role": "contact", "body": "How much does whitening cost?"}],
                     expect={"mustNotInclude": ["guarantee"], "escalate": False}, last_detail=[]),
        ScenarioTest(org_id=org.id, name="Angry patient escalates", channel="whatsapp",
                     messages=[{"role": "contact", "body": "This is the second time my filling came off. I want a refund."}],
                     expect={"escalate": True}, last_detail=[]),
    ])

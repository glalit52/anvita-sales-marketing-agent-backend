"""Reviews, review requests, segments, message templates, campaigns and sequences."""

import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select, text

from app.api.deps import DB, CurrentPrincipal, ManagerPrincipal
from app.core.errors import AppError, NotFound
from app.db.base import utcnow
from app.models import (
    Campaign,
    Contact,
    Enrollment,
    MessageTemplate,
    Review,
    ReviewRequest,
    Segment,
    Sequence,
    SequenceStep,
)
from app.schemas.common import Ok
from app.schemas.growth import (
    AudienceOut,
    CampaignIn,
    CampaignOut,
    EnrollIn,
    EnrollmentOut,
    PlatformStat,
    ReplyIn,
    ReviewIn,
    ReviewOut,
    ReviewRequestFunnel,
    ReviewRequestIn,
    ReviewRequestOut,
    ReviewSettings,
    ReviewStats,
    SegmentIn,
    SegmentOut,
    SegmentPreview,
    SequenceIn,
    SequenceOut,
    StepIn,
    StepOut,
    TemplateIn,
    TemplateOut,
    TemplateStatusIn,
)
from app.services import campaigns as campaign_svc
from app.services import reviews as review_svc
from app.services import segments as segment_svc
from app.services import sequences as seq_svc
from app.services.audit import audit
from app.services.integrations import resolve_for_org

router = APIRouter(tags=["growth"])


async def _get(db: DB, model: type, obj_id: uuid.UUID, label: str):  # noqa: ANN202
    obj = await db.get(model, obj_id)
    if obj is None:
        raise NotFound(f"{label} not found")
    return obj


# ---- Reviews ----------------------------------------------------------------------------------------


@router.get("/reviews", response_model=list[ReviewOut])
async def list_reviews(
    principal: CurrentPrincipal,
    db: DB,
    platform: str | None = None,
    status: Literal["all", "needs_reply", "replied", "flagged"] = "all",
    rating: list[int] | None = Query(None),
    q: str | None = None,
    limit: int = Query(100, le=500),
) -> list[ReviewOut]:
    cond = [Review.org_id == principal.org_id]
    if platform:
        cond.append(Review.platform == platform)
    if status == "needs_reply":
        cond.append(Review.reply.is_(None))
    elif status == "replied":
        cond.append(Review.reply.is_not(None))
    elif status == "flagged":
        cond.append(Review.flagged.is_not(None))
    if rating:
        cond.append(Review.rating.in_(rating))
    if q:
        cond.append(Review.text.ilike(f"%{q}%") | Review.author.ilike(f"%{q}%"))
    rows = await db.scalars(select(Review).where(*cond).order_by(Review.posted_at.desc()).limit(limit))
    return [ReviewOut.model_validate(r) for r in rows]


@router.get("/reviews/stats", response_model=ReviewStats)
async def review_stats(principal: CurrentPrincipal, db: DB) -> ReviewStats:
    org_id = principal.org_id
    month = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    plat = (
        await db.execute(
            select(
                Review.platform,
                func.avg(Review.rating),
                func.count(),
                func.count().filter(Review.posted_at >= month),
                func.count().filter(Review.reply.is_not(None)),
            )
            .where(Review.org_id == org_id)
            .group_by(Review.platform)
        )
    ).all()
    dist = dict(
        (
            await db.execute(select(Review.rating, func.count()).where(Review.org_id == org_id).group_by(Review.rating))
        ).all()
    )
    since = (month - timedelta(days=330)).replace(day=1)
    trend_rows = (
        await db.execute(
            select(func.date_trunc("month", Review.posted_at), func.avg(Review.rating), func.count())
            .where(Review.org_id == org_id, Review.posted_at >= since)
            .group_by(text("1"))
            .order_by(text("1"))
        )
    ).all()
    topic_rows = (
        await db.execute(
            select(
                func.unnest(Review.topics).label("topic"),
                func.count().filter(Review.sentiment == "positive"),
                func.count().filter(Review.sentiment == "negative"),
            )
            .where(Review.org_id == org_id)
            .group_by("topic")
        )
    ).all()
    total = sum(r[2] for r in plat)
    replied = sum(r[4] for r in plat)
    avg = sum(float(r[1]) * r[2] for r in plat) / total if total else 0.0
    needs = int(
        await db.scalar(select(func.count()).select_from(Review).where(Review.org_id == org_id, Review.reply.is_(None)))
        or 0
    )
    return ReviewStats(
        average=round(avg, 2),
        total=total,
        response_rate=round(replied * 100 / total) if total else 0,
        needs_reply=needs,
        platforms=[
            PlatformStat(
                platform=p,
                rating=round(float(a), 1),
                count=c,
                new_this_month=n,
                response_rate=round(rp * 100 / c) if c else 0,
            )
            for p, a, c, n, rp in plat
        ],
        distribution=[{"stars": s, "count": int(dist.get(s, 0))} for s in (5, 4, 3, 2, 1)],
        trend=[{"month": m.strftime("%b"), "avg": round(float(a), 2), "count": c} for m, a, c in trend_rows],
        topics=[{"topic": t, "positive": p, "negative": n} for t, p, n in topic_rows],
    )


@router.post("/reviews", response_model=ReviewOut, status_code=201)
async def add_review(body: ReviewIn, principal: CurrentPrincipal, db: DB) -> ReviewOut:
    """Record a review manually (e.g. from a platform without an integration)."""
    review, _ = await review_svc.ingest(
        db,
        principal.org,
        platform=body.platform,
        external_id=None,
        author=body.author,
        rating=body.rating,
        text=body.text,
        posted_at=body.posted_at or utcnow(),
        location_id=body.location_id,
    )
    await db.commit()
    return ReviewOut.model_validate(review)


@router.post("/reviews/sync", response_model=dict)
async def sync_reviews(principal: ManagerPrincipal, db: DB) -> dict:
    """Pull new reviews from the connected review platform (Google Business Profile)."""
    provider = await resolve_for_org(db, principal.org_id, "reviews")
    new = await review_svc.sync_platform(db, principal.org, provider, str(principal.org_id))
    await db.commit()
    return {"imported": new}


@router.post("/reviews/{review_id}/draft", response_model=ReviewOut)
async def redraft(review_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> ReviewOut:
    review = await _get(db, Review, review_id, "Review")
    await review_svc.triage(db, principal.org, review)
    await db.commit()
    return ReviewOut.model_validate(review)


@router.post("/reviews/{review_id}/reply", response_model=ReviewOut)
async def reply(review_id: uuid.UUID, body: ReplyIn, principal: CurrentPrincipal, db: DB) -> ReviewOut:
    review = await _get(db, Review, review_id, "Review")
    if review.platform == "google" and review.external_id:
        provider = await resolve_for_org(db, principal.org_id, "reviews")
        await provider.reply(str(principal.org_id), review.external_id, body.text)
    review.reply, review.replied_at, review.flagged = body.text, utcnow(), None
    audit(db, principal.org_id, "review.replied", actor_id=principal.user_id, entity_type="review", entity_id=review.id)
    await db.commit()
    return ReviewOut.model_validate(review)


@router.get("/reviews/settings", response_model=ReviewSettings)
async def get_review_settings(principal: CurrentPrincipal) -> ReviewSettings:
    return ReviewSettings.model_validate(
        {
            **((principal.org.settings or {}).get("reviews") or {}),
            "review_links": (principal.org.settings or {}).get("review_links") or {},
        }
    )


@router.put("/reviews/settings", response_model=ReviewSettings)
async def put_review_settings(body: ReviewSettings, principal: ManagerPrincipal, db: DB) -> ReviewSettings:
    data = body.model_dump(by_alias=False)
    links = data.pop("review_links")
    principal.org.settings = {**(principal.org.settings or {}), "reviews": data, "review_links": links}
    await db.commit()
    return body


@router.get("/review-requests", response_model=list[ReviewRequestOut])
async def list_requests(principal: CurrentPrincipal, db: DB, limit: int = Query(50, le=200)) -> list[ReviewRequestOut]:
    rows = await db.execute(
        select(ReviewRequest, Contact.name)
        .join(Contact, Contact.id == ReviewRequest.contact_id)
        .where(ReviewRequest.org_id == principal.org_id)
        .order_by(ReviewRequest.created_at.desc())
        .limit(limit)
    )
    out = []
    for r, name in rows:
        item = ReviewRequestOut.model_validate(r)
        item.contact_name = name
        out.append(item)
    return out


@router.get("/review-requests/funnel", response_model=ReviewRequestFunnel)
async def request_funnel(principal: CurrentPrincipal, db: DB, days: int = Query(30, le=365)) -> ReviewRequestFunnel:
    since = utcnow() - timedelta(days=days)
    row = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(ReviewRequest.clicked_at.is_not(None)),
                func.count().filter(ReviewRequest.rating >= 4),
                func.count().filter(ReviewRequest.status == "reviewed"),
                func.count().filter(ReviewRequest.status == "private_feedback"),
            ).where(ReviewRequest.org_id == principal.org_id, ReviewRequest.created_at >= since)
        )
    ).one()
    return ReviewRequestFunnel(sent=row[0], clicked=row[1], rated_high=row[2], reviewed=row[3], private_feedback=row[4])


@router.post("/review-requests", response_model=dict, status_code=201)
async def send_requests(body: ReviewRequestIn, principal: CurrentPrincipal, db: DB) -> dict:
    sent, skipped = 0, []
    for cid in body.contact_ids:
        contact = await db.get(Contact, cid)
        if contact is None:
            continue
        try:
            await review_svc.send_request(db, principal.org, contact, channel=body.channel, sent_by=principal.user_id)
            sent += 1
        except AppError as e:
            skipped.append({"contactId": str(cid), "reason": e.message})
    await db.commit()
    return {"sent": sent, "skipped": skipped}


# ---- Segments ---------------------------------------------------------------------------------------


@router.get("/segments", response_model=list[SegmentOut])
async def list_segments(principal: CurrentPrincipal, db: DB) -> list[SegmentOut]:
    rows = (await db.scalars(select(Segment).where(Segment.org_id == principal.org_id).order_by(Segment.name))).all()
    out = []
    for s in rows:
        item = SegmentOut.model_validate(s)
        item.count = await segment_svc.count(db, principal.org_id, s.rules)
        out.append(item)
    return out


@router.post("/segments/preview", response_model=SegmentPreview)
async def preview_segment(body: SegmentIn, principal: CurrentPrincipal, db: DB) -> SegmentPreview:
    rules = body.rules.model_dump(by_alias=False)
    sample = await segment_svc.contacts(db, principal.org_id, rules, limit=5)
    return SegmentPreview(
        count=await segment_svc.count(db, principal.org_id, rules),
        sample=[{"id": str(c.id), "name": c.name, "stage": c.stage} for c in sample],
    )


@router.post("/segments", response_model=SegmentOut, status_code=201)
async def create_segment(body: SegmentIn, principal: ManagerPrincipal, db: DB) -> SegmentOut:
    rules = body.rules.model_dump(by_alias=False)
    await segment_svc.count(db, principal.org_id, rules)  # validates the rules
    seg = Segment(org_id=principal.org_id, name=body.name, description=body.description, rules=rules)
    db.add(seg)
    await db.commit()
    out = SegmentOut.model_validate(seg)
    out.count = await segment_svc.count(db, principal.org_id, rules)
    return out


@router.put("/segments/{segment_id}", response_model=SegmentOut)
async def update_segment(segment_id: uuid.UUID, body: SegmentIn, principal: ManagerPrincipal, db: DB) -> SegmentOut:
    seg = await _get(db, Segment, segment_id, "Segment")
    seg.name, seg.description, seg.rules = body.name, body.description, body.rules.model_dump(by_alias=False)
    await db.commit()
    out = SegmentOut.model_validate(seg)
    out.count = await segment_svc.count(db, principal.org_id, seg.rules)
    return out


@router.delete("/segments/{segment_id}", response_model=Ok)
async def delete_segment(segment_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, Segment, segment_id, "Segment"))
    await db.commit()
    return Ok()


# ---- Message templates ------------------------------------------------------------------------------


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(principal: CurrentPrincipal, db: DB, channel: str | None = None) -> list[TemplateOut]:
    q = select(MessageTemplate).where(MessageTemplate.org_id == principal.org_id)
    if channel:
        q = q.where(MessageTemplate.channel == channel)
    return [TemplateOut.model_validate(t) for t in await db.scalars(q.order_by(MessageTemplate.name))]


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(body: TemplateIn, principal: ManagerPrincipal, db: DB) -> TemplateOut:
    # SMS/email templates are usable right away; WhatsApp ones need Meta approval first.
    status = "draft" if body.channel == "whatsapp" else "approved"
    t = MessageTemplate(org_id=principal.org_id, status=status, **body.model_dump(by_alias=False))
    db.add(t)
    await db.commit()
    return TemplateOut.model_validate(t)


@router.put("/templates/{template_id}", response_model=TemplateOut)
async def update_template(template_id: uuid.UUID, body: TemplateIn, principal: ManagerPrincipal, db: DB) -> TemplateOut:
    t = await _get(db, MessageTemplate, template_id, "Template")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(t, k, v)
    if t.channel == "whatsapp":
        t.status = "draft"  # edited WhatsApp templates must be re-approved
    await db.commit()
    return TemplateOut.model_validate(t)


@router.post("/templates/{template_id}/submit", response_model=TemplateOut)
async def submit_template(template_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> TemplateOut:
    """Submit a WhatsApp template for Meta review. Without Graph API template management configured, it
    waits as 'pending' until someone records Meta's decision (PUT /templates/{id}/status)."""
    t = await _get(db, MessageTemplate, template_id, "Template")
    if t.channel != "whatsapp":
        raise AppError("Only WhatsApp templates need approval", code="not_whatsapp")
    t.status = "pending"
    await db.commit()
    return TemplateOut.model_validate(t)


@router.put("/templates/{template_id}/status", response_model=TemplateOut)
async def set_template_status(
    template_id: uuid.UUID, body: TemplateStatusIn, principal: ManagerPrincipal, db: DB
) -> TemplateOut:
    t = await _get(db, MessageTemplate, template_id, "Template")
    t.status, t.rejection_reason = body.status, body.reason
    await db.commit()
    return TemplateOut.model_validate(t)


@router.delete("/templates/{template_id}", response_model=Ok)
async def delete_template(template_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, MessageTemplate, template_id, "Template"))
    await db.commit()
    return Ok()


# ---- Campaigns --------------------------------------------------------------------------------------


async def _campaign_out(db: DB, c: Campaign, with_stats: bool = True) -> CampaignOut:
    out = CampaignOut.model_validate(
        {**{k.key: getattr(c, k.key) for k in Campaign.__table__.columns}, "audience": c.audience_count}
    )
    if c.segment_id:
        seg = await db.get(Segment, c.segment_id)
        out.segment_name = seg.name if seg else None
    if c.template_id:
        t = await db.get(MessageTemplate, c.template_id)
        out.template_name = t.name if t else None
    if with_stats and c.status in ("sending", "sent"):
        out.stats = await campaign_svc.stats(db, c)
    return out


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(principal: CurrentPrincipal, db: DB) -> list[CampaignOut]:
    rows = await db.scalars(
        select(Campaign).where(Campaign.org_id == principal.org_id).order_by(Campaign.created_at.desc())
    )
    return [await _campaign_out(db, c) for c in rows]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(body: CampaignIn, principal: ManagerPrincipal, db: DB) -> CampaignOut:
    c = Campaign(
        org_id=principal.org_id, created_by_id=principal.user_id, status="draft", **body.model_dump(by_alias=False)
    )
    db.add(c)
    await db.flush()
    c.audience_count = (await campaign_svc.audience(db, c))[0]
    await db.commit()
    return await _campaign_out(db, c)


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> CampaignOut:
    return await _campaign_out(db, await _get(db, Campaign, campaign_id, "Campaign"))


@router.put("/campaigns/{campaign_id}", response_model=CampaignOut)
async def update_campaign(campaign_id: uuid.UUID, body: CampaignIn, principal: ManagerPrincipal, db: DB) -> CampaignOut:
    c = await _get(db, Campaign, campaign_id, "Campaign")
    if c.status not in ("draft", "scheduled"):
        raise AppError("Only drafts and scheduled campaigns can be edited", code="invalid_status")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(c, k, v)
    c.audience_count = (await campaign_svc.audience(db, c))[0]
    await db.commit()
    return await _campaign_out(db, c)


@router.get("/campaigns/{campaign_id}/audience", response_model=AudienceOut)
async def campaign_audience(campaign_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> AudienceOut:
    eligible, total = await campaign_svc.audience(db, await _get(db, Campaign, campaign_id, "Campaign"))
    return AudienceOut(eligible=eligible, total=total)


@router.post("/campaigns/{campaign_id}/launch", response_model=CampaignOut)
async def launch(campaign_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> CampaignOut:
    """Send now, or at scheduledAt if it is in the future. Only contacts with marketing consent on the
    channel are messaged; the rest are counted as skipped."""
    c = await _get(db, Campaign, campaign_id, "Campaign")
    await campaign_svc.start(db, c)
    audit(
        db,
        principal.org_id,
        "campaign.launched",
        actor_id=principal.user_id,
        entity_type="campaign",
        entity_id=c.id,
        data={"audience": c.audience_count},
    )
    await db.commit()
    return await _campaign_out(db, c)


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(campaign_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> CampaignOut:
    c = await _get(db, Campaign, campaign_id, "Campaign")
    if c.status not in ("scheduled", "sending"):
        raise AppError("Nothing to cancel", code="invalid_status")
    c.status = "cancelled"
    await db.commit()
    return await _campaign_out(db, c)


@router.delete("/campaigns/{campaign_id}", response_model=Ok)
async def delete_campaign(campaign_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    c = await _get(db, Campaign, campaign_id, "Campaign")
    if c.status in ("sending",):
        raise AppError("Cancel the campaign before deleting it", code="invalid_status")
    await db.delete(c)
    await db.commit()
    return Ok()


# ---- Sequences --------------------------------------------------------------------------------------


async def _sequence_out(db: DB, seq: Sequence, detail: bool = False) -> SequenceOut:
    steps = await seq_svc.steps_of(db, seq.id)
    per_step = await seq_svc.step_stats(db, seq) if detail else {}
    out = SequenceOut.model_validate(
        {**{k.key: getattr(seq, k.key) for k in Sequence.__table__.columns}, "settings": seq_svc.settings_of(seq)}
    )
    out.stats = await seq_svc.stats(db, seq)
    out.steps = [
        StepOut.model_validate(
            {**{k.key: getattr(s, k.key) for k in SequenceStep.__table__.columns}, "stats": per_step.get(s.id, {})}
        )
        for s in steps
    ]
    return out


def _replace_steps(db: DB, seq: Sequence, steps: list[StepIn]) -> None:
    for i, st in enumerate(steps):
        db.add(SequenceStep(org_id=seq.org_id, sequence_id=seq.id, position=i, **st.model_dump(by_alias=False)))


@router.get("/sequences", response_model=list[SequenceOut])
async def list_sequences(principal: CurrentPrincipal, db: DB) -> list[SequenceOut]:
    rows = await db.scalars(
        select(Sequence)
        .where(Sequence.org_id == principal.org_id)
        .order_by(case((Sequence.status == "active", 0), else_=1), Sequence.updated_at.desc())
    )
    return [await _sequence_out(db, s) for s in rows]


@router.post("/sequences", response_model=SequenceOut, status_code=201)
async def create_sequence(body: SequenceIn, principal: ManagerPrincipal, db: DB) -> SequenceOut:
    seq = Sequence(
        org_id=principal.org_id,
        name=body.name,
        audience=body.audience,
        status="draft",
        owner_id=body.owner_id or principal.user_id,
        settings=body.settings.model_dump(by_alias=False),
    )
    db.add(seq)
    await db.flush()
    _replace_steps(db, seq, body.steps)
    await db.commit()
    return await _sequence_out(db, seq, detail=True)


@router.get("/sequences/{sequence_id}", response_model=SequenceOut)
async def get_sequence(sequence_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> SequenceOut:
    return await _sequence_out(db, await _get(db, Sequence, sequence_id, "Sequence"), detail=True)


@router.put("/sequences/{sequence_id}", response_model=SequenceOut)
async def update_sequence(sequence_id: uuid.UUID, body: SequenceIn, principal: ManagerPrincipal, db: DB) -> SequenceOut:
    """Replace the sequence definition. Active enrollments continue from their next step position."""
    seq = await _get(db, Sequence, sequence_id, "Sequence")
    seq.name, seq.audience = body.name, body.audience
    seq.owner_id = body.owner_id or seq.owner_id
    seq.settings = body.settings.model_dump(by_alias=False)
    old = await seq_svc.steps_of(db, seq.id)
    for st, new in zip(old, body.steps, strict=False):  # keep ids (and their stats) for existing positions
        for k, v in new.model_dump(by_alias=False).items():
            setattr(st, k, v)
    for st in old[len(body.steps) :]:
        await db.delete(st)
    for i, new in enumerate(body.steps[len(old) :], start=len(old)):
        db.add(SequenceStep(org_id=seq.org_id, sequence_id=seq.id, position=i, **new.model_dump(by_alias=False)))
    await db.commit()
    return await _sequence_out(db, seq, detail=True)


@router.post("/sequences/{sequence_id}/status", response_model=SequenceOut)
async def set_sequence_status(
    sequence_id: uuid.UUID, principal: ManagerPrincipal, db: DB, status: Literal["active", "paused"] = Query(...)
) -> SequenceOut:
    seq = await _get(db, Sequence, sequence_id, "Sequence")
    if status == "active" and not await seq_svc.steps_of(db, seq.id):
        raise AppError("Add at least one step first", code="no_steps")
    was = seq.status
    seq.status = status
    if status == "active" and was != "active":
        await seq_svc.resume(db, principal.org, seq)
    audit(
        db, principal.org_id, f"sequence.{status}", actor_id=principal.user_id, entity_type="sequence", entity_id=seq.id
    )
    await db.commit()
    return await _sequence_out(db, seq, detail=True)


@router.delete("/sequences/{sequence_id}", response_model=Ok)
async def delete_sequence(sequence_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    await db.delete(await _get(db, Sequence, sequence_id, "Sequence"))
    await db.commit()
    return Ok()


@router.post("/sequences/{sequence_id}/enroll", response_model=dict)
async def enroll(sequence_id: uuid.UUID, body: EnrollIn, principal: CurrentPrincipal, db: DB) -> dict:
    seq = await _get(db, Sequence, sequence_id, "Sequence")
    result = await seq_svc.enroll(db, principal.org, seq, body.contact_ids, principal.user_id)
    await db.commit()
    return result


@router.get("/sequences/{sequence_id}/enrollments", response_model=list[EnrollmentOut])
async def enrollments(
    sequence_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DB,
    status: str | None = None,
    limit: int = Query(200, le=1000),
) -> list[EnrollmentOut]:
    q = (
        select(Enrollment, Contact.name)
        .join(Contact, Contact.id == Enrollment.contact_id)
        .where(Enrollment.sequence_id == sequence_id)
    )
    if status:
        q = q.where(Enrollment.status == status)
    rows = await db.execute(q.order_by(Enrollment.created_at.desc()).limit(limit))
    out = []
    for e, name in rows:
        item = EnrollmentOut.model_validate(e)
        item.contact_name = name
        out.append(item)
    return out


@router.post("/sequences/{sequence_id}/enrollments/{enrollment_id}/stop", response_model=Ok)
async def stop_enrollment(sequence_id: uuid.UUID, enrollment_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    e = await _get(db, Enrollment, enrollment_id, "Enrollment")
    if e.status == "active":
        e.status, e.stop_reason, e.next_run_at = "stopped", f"Stopped by {principal.user.name}", None
    await db.commit()
    return Ok()


@router.get("/sequences-meta/mailboxes", response_model=list[dict])
async def mailboxes(principal: CurrentPrincipal, db: DB) -> list[dict]:
    """Email channels used to send sequences, with today's volume against their daily cap."""
    from app.models import Channel, Message

    rows = (await db.scalars(select(Channel).where(Channel.org_id == principal.org_id, Channel.type == "email"))).all()
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent = int(
        await db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.org_id == principal.org_id,
                Message.direction == "outbound",
                Message.created_at >= today,
                Message.author_name.like("%(sequence)"),
            )
        )
        or 0
    )
    return [
        {
            "id": str(c.id),
            "address": c.external_id or c.name,
            "provider": (c.config or {}).get("provider", "SMTP"),
            "status": c.status,
            "dailyCap": int((c.config or {}).get("daily_cap", 60)),
            "sentToday": sent,
        }
        for c in rows
    ]

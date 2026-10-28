"""Unauthenticated endpoints used by customers: booking page, pay page and the webchat widget."""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import select

from app.api.deps import DB
from app.core.config import get_settings
from app.core.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized
from app.core.ratelimit import client_ip, limiter
from app.core.security import create_webchat_token, decode_jwt
from app.db.base import utcnow
from app.db.session import set_tenant
from app.models import (
    AgentConfig,
    Contact,
    Conversation,
    LandingPage,
    Location,
    Message,
    Organization,
    Payment,
    Provider,
    ReviewRequest,
    Service,
)
from app.schemas.public import (
    FeedbackPage,
    FeedbackPrivateIn,
    FeedbackRateIn,
    FeedbackRateOut,
    LandingPublic,
    LandingSubmitIn,
    LandingSubmitOut,
    PublicBookingIn,
    PublicBookingOut,
    PublicBusiness,
    PublicLocation,
    PublicPayment,
    PublicProvider,
    PublicService,
    TestConfirmIn,
    WebchatMessage,
    WebchatMessageIn,
    WebchatSessionIn,
    WebchatSessionOut,
)
from app.schemas.scheduling import Slot
from app.services import marketing, reviews, scheduling
from app.services.contacts import get_or_create_contact, set_consent
from app.services.events import emit
from app.services.inbox import open_conversation, record_inbound
from app.services.notify import notify
from app.services.payments import create_payment, mark_paid, pay_url

router = APIRouter(prefix="/public", tags=["public"])


def _throttle(request: Request) -> None:
    limiter.check(f"public:{client_ip(request)}", get_settings().rate_limit_public_per_minute)


Throttled = Depends(_throttle)


async def _org(db: DB, slug: str) -> Organization:
    org = await db.scalar(select(Organization).where(Organization.slug == slug))
    if org is None:
        raise NotFound("Business not found")
    await set_tenant(db, org.id)
    return org


@router.get("/orgs/{slug}", response_model=PublicBusiness, dependencies=[Throttled])
async def business(slug: str, db: DB) -> PublicBusiness:
    org = await _org(db, slug)
    locs = await db.scalars(select(Location).where(Location.org_id == org.id, Location.active.is_(True)))
    svcs = await db.scalars(
        select(Service)
        .where(Service.org_id == org.id, Service.active.is_(True), Service.online.is_(True))
        .order_by(Service.name)
    )
    provs = await db.scalars(select(Provider).where(Provider.org_id == org.id, Provider.active.is_(True)))
    public_profile = {
        k: v
        for k, v in (org.business_profile or {}).items()
        if k in ("description", "website", "phone", "address", "hours", "logoUrl", "rating")
    }
    return PublicBusiness(
        name=org.name,
        slug=org.slug,
        timezone=org.timezone,
        currency=org.currency,
        profile=public_profile,
        locations=[PublicLocation.model_validate(loc) for loc in locs],
        services=[PublicService.model_validate(s) for s in svcs],
        providers=[PublicProvider.model_validate(p) for p in provs],
    )


@router.get("/orgs/{slug}/availability", response_model=list[Slot], dependencies=[Throttled])
async def public_availability(
    slug: str,
    db: DB,
    service_id: uuid.UUID,
    start: date,
    days: int = Query(7, ge=1, le=14),
    location_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
) -> list[Slot]:
    org = await _org(db, slug)
    service = await db.get(Service, service_id)
    if service is None or not service.online or not service.active:
        raise NotFound("Service not found")
    slots = await scheduling.availability(
        db, org, service, start=start, days=days, location_id=location_id, provider_id=provider_id
    )
    return [
        Slot(starts_at=s.starts_at, ends_at=s.ends_at, provider_id=s.provider_id, location_id=s.location_id)
        for s in slots
    ]


@router.post("/orgs/{slug}/bookings", response_model=PublicBookingOut, status_code=201, dependencies=[Throttled])
async def public_book(slug: str, body: PublicBookingIn, db: DB) -> PublicBookingOut:
    org = await _org(db, slug)
    service = await db.get(Service, body.service_id)
    if service is None or not service.online or not service.active:
        raise NotFound("Service not found")
    contact, _ = await get_or_create_contact(
        db,
        org.id,
        name=body.name,
        phone=body.phone,
        email=body.email,
        channel="whatsapp",
        source="Booking page",
        country=org.country,
    )
    appt = await scheduling.book(
        db,
        org,
        contact_id=contact.id,
        service_id=service.id,
        location_id=body.location_id,
        starts_at=body.starts_at,
        provider_id=body.provider_id,
        source="online",
        notes=body.notes,
        idempotency_key=f"online:{contact.id}:{service.id}:{body.starts_at.isoformat()}",
    )
    if contact.stage in ("prospect", "new", "qualified"):
        contact.stage = "booked"
    if body.whatsapp_opt_in:
        await set_consent(db, org.id, contact.id, channel="whatsapp", status="opted_in", source="Booking page")
    url = None
    if service.deposit_minor:
        payment = await create_payment(
            db,
            org,
            amount_minor=service.deposit_minor,
            purpose=f"Deposit · {service.name}",
            contact=contact,
            appointment_id=appt.id,
            created_by_type="system",
        )
        url = pay_url(payment)
    location = await db.get(Location, appt.location_id)
    provider = await db.get(Provider, appt.provider_id)
    assert location and provider
    notify(
        db,
        org.id,
        tab="system",
        icon="booking",
        title=f"New online booking · {contact.name}",
        body=f"{service.name} at {location.name}",
        contact_id=contact.id,
        link=f"/calendar?appt={appt.id}",
    )
    await db.commit()
    return PublicBookingOut(
        appointment_id=appt.id,
        starts_at=appt.starts_at,
        ends_at=appt.ends_at,
        location=location.name,
        service=service.name,
        provider=provider.name,
        pay_url=url,
    )


# ---- Pay page --------------------------------------------------------------------------------------


async def _payment_by_token(db: DB, token: str) -> tuple[Payment, Organization]:
    db.info["rls_bypass"] = True
    found = (await db.execute(select(Payment.id, Payment.org_id).where(Payment.public_token == token))).first()
    await db.rollback()
    db.info["rls_bypass"] = False
    if found is None:
        raise NotFound("Payment link not found")
    await set_tenant(db, found.org_id)
    payment = await db.get(Payment, found.id, with_for_update=True)
    org = await db.get(Organization, found.org_id)
    assert payment and org
    return payment, org


@router.get("/pay/{token}", response_model=PublicPayment, dependencies=[Throttled])
async def get_pay(token: str, db: DB) -> PublicPayment:
    p, org = await _payment_by_token(db, token)
    contact = await db.get(Contact, p.contact_id) if p.contact_id else None
    return PublicPayment(
        business_name=org.name,
        number=p.number,
        amount_minor=p.amount_minor,
        currency=p.currency,
        purpose=p.purpose,
        description=p.description,
        lines=p.lines,
        status=p.status,
        customer_name=contact.name if contact else None,
        due_at=p.due_at,
        paid_at=p.paid_at,
        provider=p.provider,
        checkout_url=p.checkout_url,
    )


@router.post("/pay/{token}/test-confirm", response_model=PublicPayment, dependencies=[Throttled])
async def test_confirm(token: str, body: TestConfirmIn, db: DB) -> PublicPayment:
    """Simulates a successful checkout for the `test` payment provider (dev, staging demos, e2e tests)."""
    s = get_settings()
    if s.is_production or s.payments_provider != "test":
        raise Forbidden("Test payments are disabled")
    p, _ = await _payment_by_token(db, token)
    if p.provider != "test":
        raise Forbidden("Not a test payment")
    if p.status not in ("pending", "paid"):
        raise AppError(f"Payment is {p.status}", code="invalid_status")
    await mark_paid(db, p, method=body.method, provider_payment_id=f"test_pay_{uuid.uuid4().hex[:12]}")
    await db.commit()
    return await get_pay(token, db)


# ---- Landing pages ---------------------------------------------------------------------------------


async def _page(db: DB, slug: str, page_slug: str) -> tuple[LandingPage, Organization]:
    org = await _org(db, slug)
    page = await db.scalar(
        select(LandingPage).where(
            LandingPage.org_id == org.id, LandingPage.slug == page_slug, LandingPage.status.in_(["live", "testing"])
        )
    )
    if page is None:
        raise NotFound("Page not found")
    return page, org


@router.get("/pages/{slug}/{page_slug}", response_model=LandingPublic, dependencies=[Throttled])
async def landing(slug: str, page_slug: str, db: DB, visitor: str = Query("anon", max_length=64)) -> LandingPublic:
    page, org = await _page(db, slug, page_slug)
    variant = marketing.pick_variant(page, visitor)
    page.views = {**(page.views or {}), variant: int((page.views or {}).get(variant, 0)) + 1}
    await db.commit()
    return LandingPublic(
        business_name=org.name,
        name=page.name,
        variant=variant,
        sections=marketing.render_sections(page, variant),
    )


@router.post(
    "/pages/{slug}/{page_slug}/submit", response_model=LandingSubmitOut, status_code=201, dependencies=[Throttled]
)
async def landing_submit(slug: str, page_slug: str, body: LandingSubmitIn, db: DB) -> LandingSubmitOut:
    page, org = await _page(db, slug, page_slug)
    if not (body.phone or body.email):
        raise AppError("Phone or email is required", code="contact_required")
    contact, created = await get_or_create_contact(
        db,
        org.id,
        name=body.name,
        phone=body.phone,
        email=body.email,
        channel="whatsapp" if body.phone else "email",
        source=f"landing:{page.slug}",
        country=org.country,
    )
    if created and body.utm:
        contact.utm = {k: str(v)[:200] for k, v in body.utm.items()}
    if body.marketing_consent:
        for ch in ("whatsapp", "sms") if contact.phone else ():
            await set_consent(db, org.id, contact.id, channel=ch, status="opted_in", source=f"Landing page {page.slug}")
        if contact.email:
            await set_consent(
                db, org.id, contact.id, channel="email", status="opted_in", source=f"Landing page {page.slug}"
            )
    v = body.variant if body.variant in ("A", "B") else "A"
    page.conversions = {**(page.conversions or {}), v: int((page.conversions or {}).get(v, 0)) + 1}
    emit(
        db,
        org.id,
        "form.submitted",
        {"contact_id": contact.id, "page_id": page.id, "campaign_id": page.campaign_id, "message": body.message},
    )
    notify(
        db,
        org.id,
        tab="system",
        icon="campaign",
        title=f"New lead from {page.name}",
        body=(body.message or "")[:300],
        contact_id=contact.id,
        link=f"/contacts/{contact.id}",
    )
    await db.commit()
    return LandingSubmitOut(ok=True, message="Thanks! We'll be in touch shortly.")


# ---- Review feedback page ----------------------------------------------------------------------------


async def _request_by_token(db: DB, token: str) -> tuple[ReviewRequest, Organization]:
    db.info["rls_bypass"] = True
    found = (
        await db.execute(select(ReviewRequest.id, ReviewRequest.org_id).where(ReviewRequest.token == token))
    ).first()
    await db.rollback()
    db.info["rls_bypass"] = False
    if found is None:
        raise NotFound("Feedback link not found")
    await set_tenant(db, found.org_id)
    req = await db.get(ReviewRequest, found.id, with_for_update=True)
    org = await db.get(Organization, found.org_id)
    assert req and org
    return req, org


async def _feedback_page(db: DB, req: ReviewRequest, org: Organization) -> FeedbackPage:
    contact = await db.get(Contact, req.contact_id)
    return FeedbackPage(
        business_name=org.name,
        first_name=((contact.name if contact else "") or "").split(" ")[0] or None,
        status=req.status,
        rating=req.rating,
        issues=reviews.ISSUES,
        public_link=reviews.review_link(org) if (req.rating or 0) >= 4 else None,
        suggestions=req.suggestions or {},
    )


@router.get("/feedback/{token}", response_model=FeedbackPage, dependencies=[Throttled])
async def get_feedback(token: str, db: DB) -> FeedbackPage:
    req, org = await _request_by_token(db, token)
    if req.status == "sent":
        req.status, req.clicked_at = "clicked", utcnow()
        await db.commit()
    return await _feedback_page(db, req, org)


@router.post("/feedback/{token}/rate", response_model=FeedbackRateOut, dependencies=[Throttled])
async def feedback_rate(token: str, body: FeedbackRateIn, db: DB) -> FeedbackRateOut:
    req, org = await _request_by_token(db, token)
    if req.status in ("reviewed", "private_feedback"):
        raise Conflict("Feedback already submitted")
    out = await reviews.rate(db, org, req, body.rating)
    await db.commit()
    return FeedbackRateOut(public_link=out["publicLink"], suggestions=out["suggestions"])


@router.post("/feedback/{token}/private", response_model=FeedbackPage, dependencies=[Throttled])
async def feedback_private(token: str, body: FeedbackPrivateIn, db: DB) -> FeedbackPage:
    req, org = await _request_by_token(db, token)
    if req.rating is None:
        raise AppError("Rate first", code="not_rated")
    if req.status == "private_feedback":
        raise Conflict("Feedback already submitted")
    await reviews.private_feedback(db, org, req, body.issues, body.text)
    await db.commit()
    return await _feedback_page(db, req, org)


@router.post("/feedback/{token}/posted", response_model=FeedbackPage, dependencies=[Throttled])
async def feedback_posted(token: str, db: DB) -> FeedbackPage:
    """The customer clicked through to the public review site."""
    req, org = await _request_by_token(db, token)
    if (req.rating or 0) >= 4:
        req.status = "reviewed"
        await db.commit()
    return await _feedback_page(db, req, org)


# ---- Webchat widget --------------------------------------------------------------------------------


async def webchat_claims(db: DB, authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing webchat token")
    claims = decode_jwt(authorization[7:], expected_type="webchat")
    await set_tenant(db, uuid.UUID(claims["org"]))
    return claims


@router.post("/webchat/{slug}/sessions", response_model=WebchatSessionOut, status_code=201, dependencies=[Throttled])
async def webchat_session(slug: str, body: WebchatSessionIn, db: DB) -> WebchatSessionOut:
    org = await _org(db, slug)
    if body.phone or body.email:
        contact, _ = await get_or_create_contact(
            db,
            org.id,
            name=body.name,
            phone=body.phone,
            email=body.email,
            channel="webchat",
            source="Website chat",
            country=org.country,
        )
    else:
        contact = Contact(
            org_id=org.id,
            name=body.name or "Website visitor",
            channel="webchat",
            source="Website chat",
            stage="new",
            tags=[],
            utm={},
            attributes={"page_url": body.page_url} if body.page_url else {},
        )
        db.add(contact)
        await db.flush()
    conv = await open_conversation(db, org.id, contact, "webchat", None)
    config = await db.scalar(select(AgentConfig).where(AgentConfig.org_id == org.id))
    await db.commit()
    return WebchatSessionOut(
        token=create_webchat_token(org.id, conv.id, contact.id),
        conversation_id=conv.id,
        business_name=org.name,
        agent_name=config.name if config else "Assistant",
    )


def _widget_message(m: Message) -> WebchatMessage:
    author = {"contact": "visitor", "ai": "ai", "human": "human"}.get(m.author_type, "system")
    return WebchatMessage(
        id=m.id,
        author=author,
        author_name=None if author == "visitor" else m.author_name,
        body=m.body,
        card=m.card,
        created_at=m.created_at,
    )


@router.post("/webchat/messages", response_model=WebchatMessage, status_code=201, dependencies=[Throttled])
async def webchat_send(
    body: WebchatMessageIn, db: DB, claims: Annotated[dict, Depends(webchat_claims)]
) -> WebchatMessage:
    conv = await db.get(Conversation, uuid.UUID(claims["conv"]), with_for_update=True)
    contact = await db.get(Contact, uuid.UUID(claims["sub"]))
    if conv is None or contact is None:
        raise Unauthorized("Session expired")
    key = f"webchat:{conv.id}:{body.client_id}" if body.client_id else None
    _, msg = await record_inbound(
        db,
        conv.org_id,
        contact,
        channel_type="webchat",
        channel_id=conv.channel_id,
        body=body.body,
        provider_msg_id=key,
    )
    await db.commit()
    if msg is None:  # client retry of a message we already have
        msg = await db.scalar(select(Message).where(Message.provider_msg_id == key))
    assert msg is not None
    return _widget_message(msg)


@router.get("/webchat/messages", response_model=list[WebchatMessage], dependencies=[Throttled])
async def webchat_poll(
    db: DB, claims: Annotated[dict, Depends(webchat_claims)], after: datetime | None = None
) -> list[WebchatMessage]:
    q = select(Message).where(
        Message.conversation_id == uuid.UUID(claims["conv"]),
        Message.is_note.is_(False),
        Message.is_draft.is_(False),
        Message.author_type != "system",
    )
    if after:
        q = q.where(Message.created_at > after)
    rows = await db.scalars(q.order_by(Message.created_at).limit(200))
    return [_widget_message(m) for m in rows]

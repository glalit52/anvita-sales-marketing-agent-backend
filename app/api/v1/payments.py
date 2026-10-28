import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DB, CurrentPrincipal, ManagerPrincipal
from app.core.errors import AppError, NotFound
from app.db.base import utcnow
from app.models import Contact, Payment
from app.schemas.common import Page
from app.schemas.payments import PaymentIn, PaymentOut, PaymentSummary
from app.services.audit import audit
from app.services.payments import create_payment, mark_paid, pay_url

router = APIRouter(prefix="/payments", tags=["payments"])


def payment_out(p: Payment) -> PaymentOut:
    data = {c.key: getattr(p, c.key) for c in Payment.__table__.columns}
    status = "overdue" if p.status == "pending" and p.due_at and p.due_at < utcnow() else p.status
    return PaymentOut.model_validate({**data, "status": status, "pay_url": pay_url(p)})


@router.get("", response_model=Page[PaymentOut])
async def list_payments(
    principal: CurrentPrincipal,
    db: DB,
    status: list[str] | None = Query(None),
    contact_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[PaymentOut]:
    cond = [Payment.org_id == principal.org_id]
    if status:
        cond.append(Payment.status.in_(status))
    if contact_id:
        cond.append(Payment.contact_id == contact_id)
    if since:
        cond.append(Payment.created_at >= since)
    total = await db.scalar(select(func.count()).select_from(Payment).where(*cond)) or 0
    rows = await db.scalars(
        select(Payment).where(*cond).order_by(Payment.created_at.desc()).limit(limit).offset(offset)
    )
    return Page(items=[payment_out(p) for p in rows], total=total, limit=limit, offset=offset)


@router.get("/summary", response_model=PaymentSummary)
async def summary(principal: CurrentPrincipal, db: DB, since: datetime | None = None) -> PaymentSummary:
    cond = [Payment.org_id == principal.org_id]
    if since:
        cond.append(Payment.created_at >= since)
    now = utcnow()
    overdue = (Payment.status == "pending") & (Payment.due_at < now)
    amt = Payment.amount_minor
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(amt).filter(Payment.status == "paid"), 0),
                func.coalesce(func.sum(amt).filter(Payment.status == "pending"), 0),
                func.coalesce(func.sum(amt).filter(overdue), 0),
                func.coalesce(func.sum(amt).filter(Payment.status == "refunded"), 0),
                func.count().filter(Payment.status == "paid"),
                func.count().filter(Payment.status == "pending"),
            ).where(*cond)
        )
    ).one()
    return PaymentSummary(
        collected_minor=row[0],
        pending_minor=row[1],
        overdue_minor=row[2],
        refunded_minor=row[3],
        paid_count=row[4],
        pending_count=row[5],
    )


@router.post("", response_model=PaymentOut, status_code=201)
async def create(body: PaymentIn, principal: CurrentPrincipal, db: DB) -> PaymentOut:
    contact = None
    if body.contact_id:
        contact = await db.get(Contact, body.contact_id)
        if contact is None:
            raise NotFound("Contact not found")
    p = await create_payment(
        db,
        principal.org,
        amount_minor=body.amount_minor,
        purpose=body.purpose,
        contact=contact,
        lines=[line.model_dump(by_alias=True) for line in body.lines],
        description=body.description,
        appointment_id=body.appointment_id,
        deal_id=body.deal_id,
        due_at=body.due_at,
        created_by_type="user",
        created_by_id=principal.user_id,
    )
    audit(
        db,
        principal.org_id,
        "payment.created",
        actor_id=principal.user_id,
        entity_type="payment",
        entity_id=p.id,
        data={"amount_minor": p.amount_minor},
    )
    await db.commit()
    return payment_out(p)


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> PaymentOut:
    p = await db.get(Payment, payment_id)
    if p is None:
        raise NotFound("Payment not found")
    return payment_out(p)


@router.post("/{payment_id}/mark-paid", response_model=PaymentOut)
async def mark_paid_manually(
    payment_id: uuid.UUID, principal: ManagerPrincipal, db: DB, method: str = "cash"
) -> PaymentOut:
    """Record an offline payment (cash, bank transfer) against a pending link."""
    p = await db.get(Payment, payment_id, with_for_update=True)
    if p is None:
        raise NotFound("Payment not found")
    if p.status not in ("pending", "overdue"):
        raise AppError(f"Cannot mark a {p.status} payment as paid", code="invalid_status")
    await mark_paid(db, p, method=method, provider_payment_id=None)
    audit(
        db,
        principal.org_id,
        "payment.marked_paid",
        actor_id=principal.user_id,
        entity_type="payment",
        entity_id=p.id,
        data={"method": method},
    )
    await db.commit()
    return payment_out(p)


@router.post("/{payment_id}/cancel", response_model=PaymentOut)
async def cancel(payment_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> PaymentOut:
    p = await db.get(Payment, payment_id, with_for_update=True)
    if p is None:
        raise NotFound("Payment not found")
    if p.status != "pending":
        raise AppError("Only pending payments can be cancelled", code="invalid_status")
    p.status = "cancelled"
    await db.commit()
    return payment_out(p)


@router.post("/{payment_id}/refund", response_model=PaymentOut)
async def refund(payment_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> PaymentOut:
    """Marks the payment refunded. With Razorpay, issue the refund in the dashboard first; automated
    provider refunds come with the payments phase."""
    p = await db.get(Payment, payment_id, with_for_update=True)
    if p is None:
        raise NotFound("Payment not found")
    if p.status != "paid":
        raise AppError("Only paid payments can be refunded", code="invalid_status")
    p.status, p.refunded_at = "refunded", utcnow()
    audit(db, principal.org_id, "payment.refunded", actor_id=principal.user_id, entity_type="payment", entity_id=p.id)
    await db.commit()
    return payment_out(p)

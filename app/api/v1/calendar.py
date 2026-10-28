import uuid
from datetime import date, datetime

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, AdminPrincipal, CurrentPrincipal
from app.core.errors import AppError, NotFound
from app.db.base import utcnow
from app.models import Appointment, Contact, Provider, Service
from app.schemas.common import Ok
from app.schemas.scheduling import (
    AppointmentIn,
    AppointmentOut,
    AppointmentUpdate,
    ProviderIn,
    ProviderOut,
    ServiceIn,
    ServiceOut,
    Slot,
)
from app.services import scheduling
from app.services.audit import audit
from app.services.events import emit

router = APIRouter(tags=["calendar"])


@router.get("/services", response_model=list[ServiceOut])
async def list_services(principal: CurrentPrincipal, db: DB) -> list[ServiceOut]:
    rows = await db.scalars(select(Service).where(Service.org_id == principal.org_id).order_by(Service.name))
    return [ServiceOut.model_validate(r) for r in rows]


@router.post("/services", response_model=ServiceOut, status_code=201)
async def create_service(body: ServiceIn, principal: AdminPrincipal, db: DB) -> ServiceOut:
    svc = Service(org_id=principal.org_id, **body.model_dump(by_alias=False))
    db.add(svc)
    await db.commit()
    return ServiceOut.model_validate(svc)


@router.put("/services/{service_id}", response_model=ServiceOut)
async def update_service(service_id: uuid.UUID, body: ServiceIn, principal: AdminPrincipal, db: DB) -> ServiceOut:
    svc = await db.get(Service, service_id)
    if svc is None:
        raise NotFound("Service not found")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(svc, k, v)
    await db.commit()
    return ServiceOut.model_validate(svc)


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(principal: CurrentPrincipal, db: DB) -> list[ProviderOut]:
    rows = await db.scalars(select(Provider).where(Provider.org_id == principal.org_id).order_by(Provider.name))
    return [ProviderOut.model_validate(r) for r in rows]


@router.post("/providers", response_model=ProviderOut, status_code=201)
async def create_provider(body: ProviderIn, principal: AdminPrincipal, db: DB) -> ProviderOut:
    p = Provider(org_id=principal.org_id, **body.model_dump(by_alias=False))
    db.add(p)
    await db.commit()
    return ProviderOut.model_validate(p)


@router.put("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: uuid.UUID, body: ProviderIn, principal: AdminPrincipal, db: DB) -> ProviderOut:
    p = await db.get(Provider, provider_id)
    if p is None:
        raise NotFound("Provider not found")
    for k, v in body.model_dump(by_alias=False).items():
        setattr(p, k, v)
    await db.commit()
    return ProviderOut.model_validate(p)


@router.get("/availability", response_model=list[Slot])
async def get_availability(
    principal: CurrentPrincipal,
    db: DB,
    service_id: uuid.UUID,
    start: date,
    days: int = Query(7, ge=1, le=31),
    location_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
) -> list[Slot]:
    service = await db.get(Service, service_id)
    if service is None:
        raise NotFound("Service not found")
    slots = await scheduling.availability(
        db, principal.org, service, start=start, days=days, location_id=location_id, provider_id=provider_id
    )
    return [
        Slot(starts_at=s.starts_at, ends_at=s.ends_at, provider_id=s.provider_id, location_id=s.location_id)
        for s in slots
    ]


@router.get("/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    principal: CurrentPrincipal,
    db: DB,
    start: datetime,
    end: datetime,
    location_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
) -> list[AppointmentOut]:
    if end <= start:
        raise AppError("end must be after start", code="invalid_range")
    q = select(Appointment).where(
        Appointment.org_id == principal.org_id, Appointment.starts_at < end, Appointment.ends_at > start
    )
    if location_id:
        q = q.where(Appointment.location_id == location_id)
    if provider_id:
        q = q.where(Appointment.provider_id == provider_id)
    if contact_id:
        q = q.where(Appointment.contact_id == contact_id)
    rows = await db.scalars(q.order_by(Appointment.starts_at))
    return [AppointmentOut.model_validate(r) for r in rows]


@router.post("/appointments", response_model=AppointmentOut, status_code=201)
async def create_appointment(body: AppointmentIn, principal: CurrentPrincipal, db: DB) -> AppointmentOut:
    contact = await db.get(Contact, body.contact_id)
    if contact is None:
        raise NotFound("Contact not found")
    appt = await scheduling.book(
        db,
        principal.org,
        contact_id=body.contact_id,
        service_id=body.service_id,
        location_id=body.location_id,
        starts_at=body.starts_at,
        provider_id=body.provider_id,
        source="staff",
        status=body.status,
        notes=body.notes,
        deal_id=body.deal_id,
        idempotency_key=body.idempotency_key,
    )
    if contact.stage in ("prospect", "new", "qualified"):
        contact.stage = "booked"
    audit(
        db,
        principal.org_id,
        "appointment.booked",
        actor_id=principal.user_id,
        entity_type="appointment",
        entity_id=appt.id,
    )
    await db.commit()
    return AppointmentOut.model_validate(appt)


@router.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
async def update_appointment(
    appointment_id: uuid.UUID, body: AppointmentUpdate, principal: CurrentPrincipal, db: DB
) -> AppointmentOut:
    appt = await db.get(Appointment, appointment_id, with_for_update=True)
    if appt is None:
        raise NotFound("Appointment not found")
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    if "starts_at" in changes or "provider_id" in changes:
        # Reschedule = cancel + rebook, so the overlap constraint and working-hours rules apply.
        appt.status, appt.cancelled_at, appt.cancel_reason = "cancelled", utcnow(), "Rescheduled"
        await db.flush()
        new = await scheduling.book(
            db,
            principal.org,
            contact_id=appt.contact_id,
            service_id=appt.service_id,
            location_id=appt.location_id,
            starts_at=changes.get("starts_at", appt.starts_at),
            provider_id=changes.get("provider_id", appt.provider_id),
            source="staff",
            status=changes.get("status", "confirmed"),
            notes=changes.get("notes", appt.notes),
            deal_id=appt.deal_id,
            conversation_id=appt.conversation_id,
        )
        audit(
            db,
            principal.org_id,
            "appointment.rescheduled",
            actor_id=principal.user_id,
            entity_type="appointment",
            entity_id=new.id,
            data={"previous": str(appt.id)},
        )
        await db.commit()
        return AppointmentOut.model_validate(new)
    if "status" in changes:
        if changes["status"] == "cancelled":
            scheduling.cancel(db, appt, changes.get("cancel_reason"))
        appt.status = changes["status"]
        if appt.status == "completed":
            delay_h = float(((principal.org.settings or {}).get("reviews") or {}).get("delay_hours", 2))
            emit(
                db, principal.org_id, "appointment.completed", {"appointment_id": appt.id}, delay_seconds=delay_h * 3600
            )
            contact = await db.get(Contact, appt.contact_id)
            if contact and contact.stage in ("new", "qualified", "booked"):
                contact.stage = "customer"
    if "notes" in changes:
        appt.notes = changes["notes"]
    await db.commit()
    return AppointmentOut.model_validate(appt)


@router.delete("/appointments/{appointment_id}", response_model=Ok)
async def cancel_appointment(
    appointment_id: uuid.UUID, principal: CurrentPrincipal, db: DB, reason: str | None = None
) -> Ok:
    appt = await db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFound("Appointment not found")
    scheduling.cancel(db, appt, reason)
    await db.commit()
    return Ok()

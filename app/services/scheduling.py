import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, Conflict, NotFound
from app.db.base import utcnow
from app.models import Appointment, Location, Organization, Provider, Service

SLOT_STEP_MINUTES = 30
MIN_LEAD_MINUTES = 30
INACTIVE_STATUSES = ("cancelled", "no-show")


@dataclass(slots=True, frozen=True)
class SlotResult:
    starts_at: datetime
    ends_at: datetime
    provider_id: uuid.UUID
    location_id: uuid.UUID


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _tz(org: Organization, location: Location) -> ZoneInfo:
    return ZoneInfo(location.timezone or org.timezone or "UTC")


def _offers(provider: Provider, service_id: uuid.UUID, location_id: uuid.UUID) -> bool:
    return (not provider.service_ids or service_id in provider.service_ids) and (
        not provider.location_ids or location_id in provider.location_ids
    )


async def availability(
    db: AsyncSession,
    org: Organization,
    service: Service,
    *,
    start: date,
    days: int = 7,
    location_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
    limit: int | None = None,
) -> list[SlotResult]:
    """Free slots for a service. With no provider given, each start time is offered once per location,
    with the first free provider attached."""
    loc_q = select(Location).where(Location.org_id == org.id, Location.active.is_(True))
    if location_id:
        loc_q = loc_q.where(Location.id == location_id)
    locations = list((await db.scalars(loc_q.order_by(Location.name))).all())
    prov_q = select(Provider).where(Provider.org_id == org.id, Provider.active.is_(True))
    if provider_id:
        prov_q = prov_q.where(Provider.id == provider_id)
    providers = list((await db.scalars(prov_q.order_by(Provider.name))).all())
    if not locations or not providers:
        return []

    window_start = datetime.combine(start, time.min, tzinfo=UTC) - timedelta(days=1)
    window_end = datetime.combine(start + timedelta(days=days), time.min, tzinfo=UTC) + timedelta(days=1)
    busy_rows = await db.execute(
        select(Appointment.provider_id, Appointment.starts_at, Appointment.ends_at).where(
            Appointment.org_id == org.id,
            Appointment.provider_id.in_([p.id for p in providers]),
            Appointment.status.notin_(INACTIVE_STATUSES),
            Appointment.ends_at > window_start,
            Appointment.starts_at < window_end,
        )
    )
    busy: dict[uuid.UUID, list[tuple[datetime, datetime]]] = {}
    for pid, s, e in busy_rows:
        busy.setdefault(pid, []).append((s, e))

    earliest = utcnow() + timedelta(minutes=MIN_LEAD_MINUTES)
    duration = timedelta(minutes=service.duration_min)
    results: list[SlotResult] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for loc in locations:
            tz = _tz(org, loc)
            hours = next((h for h in loc.working_hours if h.get("day") == day.weekday()), None)
            if not hours or not hours.get("open", True):
                continue
            cursor = datetime.combine(day, _hm(hours.get("from", "09:00")), tzinfo=tz)
            close = datetime.combine(day, _hm(hours.get("to", "18:00")), tzinfo=tz)
            candidates = [p for p in providers if _offers(p, service.id, loc.id)]
            while cursor + duration <= close:
                end = cursor + duration
                if cursor >= earliest:
                    for p in candidates:
                        if all(not (cursor < e and s < end) for s, e in busy.get(p.id, [])):
                            results.append(SlotResult(cursor.astimezone(UTC), end.astimezone(UTC), p.id, loc.id))
                            break
                    if limit and len(results) >= limit:
                        return results
                cursor += timedelta(minutes=SLOT_STEP_MINUTES)
    return results


async def book(
    db: AsyncSession,
    org: Organization,
    *,
    contact_id: uuid.UUID,
    service_id: uuid.UUID,
    location_id: uuid.UUID,
    starts_at: datetime,
    provider_id: uuid.UUID | None = None,
    source: str = "staff",
    status: str = "confirmed",
    notes: str | None = None,
    deal_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> Appointment:
    if starts_at.tzinfo is None:
        raise AppError("startsAt must include a timezone offset", code="naive_datetime")
    if idempotency_key:
        existing = await db.scalar(
            select(Appointment).where(Appointment.org_id == org.id, Appointment.idempotency_key == idempotency_key)
        )
        if existing:
            return existing
    service = await db.get(Service, service_id)
    if service is None or service.org_id != org.id:
        raise NotFound("Service not found")
    starts_at = starts_at.astimezone(UTC)
    ends_at = starts_at + timedelta(minutes=service.duration_min)

    if source in ("ai", "online") or provider_id is None:
        # Customer- and AI-initiated bookings must land on a real free slot inside working hours.
        slots = await availability(
            db,
            org,
            service,
            start=starts_at.date() - timedelta(days=1),
            days=3,
            location_id=location_id,
            provider_id=provider_id,
        )
        match = next((s for s in slots if s.starts_at == starts_at), None)
        if match is None:
            raise Conflict("That time is no longer available", code="slot_unavailable")
        provider_id = match.provider_id

    appt = Appointment(
        org_id=org.id,
        contact_id=contact_id,
        service_id=service_id,
        provider_id=provider_id,
        location_id=location_id,
        deal_id=deal_id,
        conversation_id=conversation_id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=status,
        source=source,
        notes=notes,
        idempotency_key=idempotency_key,
    )
    try:
        async with db.begin_nested():
            db.add(appt)
            await db.flush()
    except IntegrityError as e:
        if "ex_appointments_no_overlap" in str(e.orig):
            raise Conflict("The provider is already booked at that time", code="slot_taken") from e
        raise
    return appt


async def overlapping(db: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime) -> list[Appointment]:
    q = select(Appointment).where(
        and_(Appointment.org_id == org_id, Appointment.starts_at < end, Appointment.ends_at > start)
    )
    return list((await db.scalars(q.order_by(Appointment.starts_at))).all())

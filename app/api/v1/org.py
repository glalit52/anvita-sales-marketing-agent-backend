import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, AdminPrincipal, CurrentPrincipal
from app.core.config import get_settings
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.security import new_token, sha256
from app.db.base import utcnow
from app.models import AuditLog, Invite, Location, Membership, User
from app.models.org import ROLE_RANK
from app.schemas.common import Ok
from app.schemas.org import (
    AuditOut,
    InviteIn,
    InviteOut,
    LocationIn,
    LocationOut,
    MemberOut,
    MemberUpdate,
    OrgOut,
    OrgUpdate,
)
from app.services.audit import audit

router = APIRouter(tags=["workspace"])
ONLINE_WINDOW = timedelta(minutes=5)


@router.get("/org", response_model=OrgOut)
async def get_org(principal: CurrentPrincipal) -> OrgOut:
    return OrgOut.model_validate(principal.org)


@router.patch("/org", response_model=OrgOut)
async def update_org(body: OrgUpdate, principal: AdminPrincipal, db: DB) -> OrgOut:
    org = principal.org
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    for k, v in changes.items():
        setattr(org, k, v.upper() if k == "currency" else v)
    audit(
        db,
        org.id,
        "org.updated",
        actor_id=principal.user_id,
        entity_type="organization",
        entity_id=org.id,
        data={"fields": sorted(changes)},
    )
    await db.commit()
    return OrgOut.model_validate(org)


# ---- Locations -------------------------------------------------------------------------------------


def _location_out(loc: Location) -> LocationOut:
    return LocationOut.model_validate(
        {
            "id": loc.id,
            "name": loc.name,
            "address": loc.address,
            "phone": loc.phone,
            "timezone": loc.timezone,
            "working_hours": loc.working_hours,
            "active": loc.active,
        }
    )


@router.get("/locations", response_model=list[LocationOut])
async def list_locations(principal: CurrentPrincipal, db: DB) -> list[LocationOut]:
    rows = await db.scalars(select(Location).where(Location.org_id == principal.org_id).order_by(Location.name))
    return [_location_out(r) for r in rows]


@router.post("/locations", response_model=LocationOut, status_code=201)
async def create_location(body: LocationIn, principal: AdminPrincipal, db: DB) -> LocationOut:
    data = body.model_dump(by_alias=False)
    data["working_hours"] = [h.model_dump(by_alias=True) for h in body.working_hours]
    loc = Location(org_id=principal.org_id, **data)
    db.add(loc)
    await db.commit()
    return _location_out(loc)


@router.put("/locations/{location_id}", response_model=LocationOut)
async def update_location(location_id: uuid.UUID, body: LocationIn, principal: AdminPrincipal, db: DB) -> LocationOut:
    loc = await db.get(Location, location_id)
    if loc is None:
        raise NotFound("Location not found")
    data = body.model_dump(by_alias=False)
    data["working_hours"] = [h.model_dump(by_alias=True) for h in body.working_hours]
    for k, v in data.items():
        setattr(loc, k, v)
    await db.commit()
    return _location_out(loc)


# ---- Team ------------------------------------------------------------------------------------------


@router.get("/team", response_model=list[MemberOut])
async def list_team(principal: CurrentPrincipal, db: DB) -> list[MemberOut]:
    rows = await db.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == principal.org_id)
        .order_by(User.name)
    )
    now = utcnow()
    return [
        MemberOut(
            id=u.id,
            name=u.name,
            email=u.email,
            role=m.role,
            location_ids=m.location_ids,
            last_seen_at=u.last_seen_at,
            online=bool(u.last_seen_at and now - u.last_seen_at < ONLINE_WINDOW),
        )
        for u, m in rows
    ]


async def _membership(db: DB, org_id: uuid.UUID, user_id: uuid.UUID) -> Membership:
    m = await db.scalar(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id))
    if m is None:
        raise NotFound("Member not found")
    return m


def _guard_role_change(principal: AdminPrincipal, target: Membership, new_role: str | None) -> None:
    if target.role == "owner" and principal.role != "owner":
        raise Forbidden("Only an owner can change another owner")
    if new_role and ROLE_RANK[new_role] > ROLE_RANK[principal.role]:
        raise Forbidden("You cannot grant a role above your own")


@router.patch("/team/{user_id}", response_model=Ok)
async def update_member(user_id: uuid.UUID, body: MemberUpdate, principal: AdminPrincipal, db: DB) -> Ok:
    m = await _membership(db, principal.org_id, user_id)
    _guard_role_change(principal, m, body.role)
    if body.role and m.role == "owner" and body.role != "owner":
        owners = await db.scalars(
            select(Membership).where(Membership.org_id == principal.org_id, Membership.role == "owner")
        )
        if len(owners.all()) <= 1:
            raise Conflict("A workspace needs at least one owner", code="last_owner")
    if body.role:
        m.role = body.role
    if body.location_ids is not None:
        m.location_ids = body.location_ids
    audit(
        db,
        principal.org_id,
        "member.updated",
        actor_id=principal.user_id,
        entity_type="user",
        entity_id=user_id,
        data=body.model_dump(mode="json", exclude_unset=True),
    )
    await db.commit()
    return Ok()


@router.delete("/team/{user_id}", response_model=Ok)
async def remove_member(user_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> Ok:
    if user_id == principal.user_id:
        raise AppError("You cannot remove yourself", code="cannot_remove_self")
    m = await _membership(db, principal.org_id, user_id)
    _guard_role_change(principal, m, None)
    await db.delete(m)
    audit(db, principal.org_id, "member.removed", actor_id=principal.user_id, entity_type="user", entity_id=user_id)
    await db.commit()
    return Ok()


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(principal: AdminPrincipal, db: DB) -> list[InviteOut]:
    rows = await db.scalars(
        select(Invite)
        .where(Invite.org_id == principal.org_id, Invite.accepted_at.is_(None))
        .order_by(Invite.created_at.desc())
    )
    return [InviteOut.model_validate(r) for r in rows]


@router.post("/invites", response_model=InviteOut, status_code=201)
async def create_invite(body: InviteIn, principal: AdminPrincipal, db: DB) -> InviteOut:
    if ROLE_RANK[body.role] > ROLE_RANK[principal.role]:
        raise Forbidden("You cannot invite someone above your own role")
    member = await db.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == principal.org_id, User.email == body.email)
    )
    if member:
        raise Conflict("That person is already in the workspace", code="already_member")
    token = new_token()
    inv = Invite(
        org_id=principal.org_id,
        email=body.email,
        role=body.role,
        token_hash=sha256(token),
        invited_by_id=principal.user_id,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(inv)
    audit(
        db,
        principal.org_id,
        "member.invited",
        actor_id=principal.user_id,
        data={"email": body.email, "role": body.role},
    )
    await db.commit()
    out = InviteOut.model_validate(inv)
    out.invite_url = f"{get_settings().app_base_url}/login?invite={token}"
    return out


@router.delete("/invites/{invite_id}", response_model=Ok)
async def revoke_invite(invite_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> Ok:
    inv = await db.get(Invite, invite_id)
    if inv is None:
        raise NotFound("Invite not found")
    await db.delete(inv)
    await db.commit()
    return Ok()


@router.get("/audit-log", response_model=list[AuditOut])
async def audit_log(
    principal: AdminPrincipal, db: DB, limit: int = Query(50, le=200), before: datetime | None = None
) -> list[AuditOut]:
    q = select(AuditLog).where(AuditLog.org_id == principal.org_id)
    if before:
        q = q.where(AuditLog.created_at < before)
    rows = await db.scalars(q.order_by(AuditLog.created_at.desc()).limit(limit))
    return [AuditOut.model_validate(r) for r in rows]

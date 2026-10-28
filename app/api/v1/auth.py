import re
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, CurrentPrincipal
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, Unauthorized
from app.core.ratelimit import client_ip, limiter
from app.core.security import create_access_token, hash_password, new_token, sha256, verify_password
from app.db.base import utcnow
from app.db.session import set_tenant
from app.models import Invite, Membership, Organization, RefreshToken, User
from app.schemas.auth import (
    AcceptInviteIn,
    LoginIn,
    MeOut,
    OrgSummary,
    RefreshIn,
    SessionOut,
    SignupIn,
    SwitchOrgIn,
    UserOut,
)
from app.schemas.common import Ok
from app.services.agent.runtime import ensure_agent_config
from app.services.audit import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "workspace"
    return slug


async def _unique_slug(db: DB, name: str) -> str:
    base = _slugify(name)
    slug = base
    while await db.scalar(select(Organization.id).where(Organization.slug == slug)):
        slug = f"{base}-{secrets.token_hex(2)}"
    return slug


async def _orgs_for(db: DB, user_id: uuid.UUID) -> list[OrgSummary]:
    rows = await db.execute(
        select(Organization, Membership.role)
        .join(Membership, Membership.org_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.name)
    )
    return [OrgSummary(id=o.id, name=o.name, slug=o.slug, role=role) for o, role in rows]


async def _issue(db: DB, request: Request, user: User, org_id: uuid.UUID) -> SessionOut:
    orgs = await _orgs_for(db, user.id)
    current = next((o for o in orgs if o.id == org_id), None)
    if current is None:
        raise Unauthorized("Not a member of this organisation")
    refresh = new_token()
    s = get_settings()
    db.add(
        RefreshToken(
            user_id=user.id,
            org_id=org_id,
            token_hash=sha256(refresh),
            expires_at=utcnow() + timedelta(days=s.refresh_token_ttl_days),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    user.last_login_at = utcnow()
    await db.commit()
    return SessionOut(
        access_token=create_access_token(user.id, org_id, current.role),
        refresh_token=refresh,
        expires_in=s.access_token_ttl_minutes * 60,
        user=UserOut.model_validate(user),
        org=current,
        orgs=orgs,
    )


@router.post("/signup", response_model=SessionOut, status_code=201)
async def signup(body: SignupIn, request: Request, db: DB) -> SessionOut:
    """Create a user, their workspace, and make them its owner."""
    limiter.check(f"auth:{client_ip(request)}", get_settings().rate_limit_auth_per_minute)
    if await db.scalar(select(User.id).where(User.email == body.email)):
        raise Conflict("An account with this email already exists", code="email_taken")
    user = User(email=body.email, name=body.name, password_hash=hash_password(body.password))
    org = Organization(
        name=body.business_name,
        slug=await _unique_slug(db, body.business_name),
        timezone=body.timezone,
        currency=body.currency.upper(),
        industry=body.industry,
        business_profile={},
        settings={},
    )
    db.add_all([user, org])
    try:
        await db.flush()
    except IntegrityError as e:
        raise Conflict("An account with this email already exists", code="email_taken") from e
    db.add(Membership(org_id=org.id, user_id=user.id, role="owner", location_ids=[]))
    await set_tenant(db, org.id)
    await ensure_agent_config(db, org)
    audit(db, org.id, "org.created", actor_id=user.id, entity_type="organization", entity_id=org.id)
    await db.flush()
    return await _issue(db, request, user, org.id)


@router.post("/login", response_model=SessionOut)
async def login(body: LoginIn, request: Request, db: DB) -> SessionOut:
    limiter.check(f"auth:{client_ip(request)}", get_settings().rate_limit_auth_per_minute)
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    org_id = body.org_id
    if org_id is None:
        org_id = await db.scalar(
            select(Membership.org_id).where(Membership.user_id == user.id).order_by(Membership.created_at).limit(1)
        )
        if org_id is None:
            raise Unauthorized("User has no workspace")
    return await _issue(db, request, user, org_id)


@router.post("/refresh", response_model=SessionOut)
async def refresh(body: RefreshIn, request: Request, db: DB) -> SessionOut:
    """Rotate the refresh token. Re-using a revoked token revokes every session for that user."""
    tok = await db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == sha256(body.refresh_token)).with_for_update()
    )
    if tok is None:
        raise Unauthorized("Invalid refresh token")
    if tok.revoked_at is not None:
        # Token reuse means it leaked: kill the whole family.
        for t in (
            await db.scalars(
                select(RefreshToken).where(RefreshToken.user_id == tok.user_id, RefreshToken.revoked_at.is_(None))
            )
        ).all():
            t.revoked_at = utcnow()
        await db.commit()
        raise Unauthorized("Refresh token reuse detected; please sign in again", code="token_reused")
    if tok.expires_at < utcnow():
        raise Unauthorized("Refresh token expired", code="token_expired")
    tok.revoked_at = utcnow()
    user = await db.get(User, tok.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("User disabled")
    return await _issue(db, request, user, tok.org_id)


@router.post("/logout", response_model=Ok)
async def logout(body: RefreshIn, db: DB) -> Ok:
    tok = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == sha256(body.refresh_token)))
    if tok and tok.revoked_at is None:
        tok.revoked_at = utcnow()
        await db.commit()
    return Ok()


@router.post("/switch-org", response_model=SessionOut)
async def switch_org(body: SwitchOrgIn, principal: CurrentPrincipal, request: Request, db: DB) -> SessionOut:
    return await _issue(db, request, principal.user, body.org_id)


@router.get("/me", response_model=MeOut)
async def me(principal: CurrentPrincipal, db: DB) -> MeOut:
    principal.user.last_seen_at = utcnow()
    await db.commit()
    orgs = await _orgs_for(db, principal.user_id)
    current = next(o for o in orgs if o.id == principal.org_id)
    return MeOut(user=UserOut.model_validate(principal.user), org=current, orgs=orgs)


@router.post("/accept-invite", response_model=SessionOut)
async def accept_invite(body: AcceptInviteIn, request: Request, db: DB) -> SessionOut:
    limiter.check(f"auth:{client_ip(request)}", get_settings().rate_limit_auth_per_minute)
    # Invites are tenant rows; look the token up across tenants, then scope to its org.
    db.info["rls_bypass"] = True
    found = (await db.execute(select(Invite.id, Invite.org_id).where(Invite.token_hash == sha256(body.token)))).first()
    await db.rollback()
    db.info["rls_bypass"] = False
    if found is None:
        raise NotFound("Invite is invalid or has expired", code="invite_invalid")
    await set_tenant(db, found.org_id)
    invite = await db.get(Invite, found.id, with_for_update=True)
    if invite is None or invite.accepted_at is not None or invite.expires_at < utcnow():
        raise NotFound("Invite is invalid or has expired", code="invite_invalid")
    user = await db.scalar(select(User).where(User.email == invite.email))
    if user is None:
        if not body.password or not body.name:
            raise Conflict("Name and password are required to create your account", code="account_required")
        user = User(email=invite.email, name=body.name, password_hash=hash_password(body.password))
        db.add(user)
        await db.flush()
    elif not body.password or not verify_password(body.password, user.password_hash):
        raise Unauthorized("Sign in with your existing password to accept", code="invalid_credentials")
    exists = await db.scalar(
        select(Membership.id).where(Membership.org_id == invite.org_id, Membership.user_id == user.id)
    )
    if not exists:
        db.add(Membership(org_id=invite.org_id, user_id=user.id, role=invite.role, location_ids=[]))
    invite.accepted_at = utcnow()
    audit(
        db,
        invite.org_id,
        "member.joined",
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        data={"role": invite.role},
    )
    await db.flush()
    return await _issue(db, request, user, invite.org_id)

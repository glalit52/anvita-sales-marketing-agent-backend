import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, Unauthorized
from app.core.logging import org_id_var
from app.core.security import decode_jwt, sha256
from app.db.base import utcnow
from app.db.session import sessionmaker, set_tenant
from app.models import Membership, Organization, User
from app.models.org import ROLE_RANK

_bearer = HTTPBearer(auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


DB = Annotated[AsyncSession, Depends(get_db)]


@dataclass(slots=True)
class Principal:
    user: User
    org: Organization
    membership: Membership

    @property
    def org_id(self) -> uuid.UUID:
        return self.org.id

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> str:
        return self.membership.role

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]


API_KEY_PREFIX = "anv_"


def required_scope(request: Request) -> str:
    """API keys are scoped per resource and verb, e.g. GET /api/v1/contacts → "contacts:read"."""
    parts = [p for p in request.url.path.split("/") if p]
    resource = parts[2] if len(parts) > 2 and parts[0] == "api" else (parts[0] if parts else "")
    return f"{resource}:{'read' if request.method in ('GET', 'HEAD') else 'write'}"


async def _api_key_principal(request: Request, db: AsyncSession, raw: str) -> Principal:
    from app.models import ApiKey

    # Keys are tenant rows; find the key across tenants, then continue scoped to its workspace.
    db.info["rls_bypass"] = True
    found = (
        await db.execute(
            select(ApiKey.id, ApiKey.org_id, ApiKey.scopes, ApiKey.created_by_id).where(
                ApiKey.key_hash == sha256(raw), ApiKey.revoked_at.is_(None)
            )
        )
    ).first()
    await db.rollback()
    db.info["rls_bypass"] = False
    if found is None or found.created_by_id is None:
        raise Unauthorized("Invalid API key")
    scope = required_scope(request)
    if "*" not in found.scopes and scope not in found.scopes:
        raise Forbidden(f"API key lacks the {scope} scope", code="insufficient_scope")
    await set_tenant(db, found.org_id)
    row = (
        await db.execute(
            select(User, Organization, Membership)
            .join(Membership, Membership.user_id == User.id)
            .join(Organization, Organization.id == Membership.org_id)
            .where(User.id == found.created_by_id, Organization.id == found.org_id)
        )
    ).first()
    if row is None or not row.User.is_active:
        raise Unauthorized("The API key's owner is no longer in this workspace")
    key = await db.get(ApiKey, found.id)
    if key is not None:
        key.last_used_at = utcnow()
    membership = row.Membership
    # Keys never act above manager, whatever their creator's role.
    if ROLE_RANK[membership.role] > ROLE_RANK["manager"]:
        membership = Membership(
            org_id=membership.org_id, user_id=membership.user_id, role="manager", location_ids=membership.location_ids
        )
    org_id_var.set(str(found.org_id))
    request.state.org_id = found.org_id
    request.state.api_key = True
    return Principal(user=row.User, org=row.Organization, membership=membership)


async def get_principal(
    request: Request,
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    api_key = request.headers.get("x-api-key")
    if api_key or (creds and creds.credentials.startswith(API_KEY_PREFIX)):
        return await _api_key_principal(request, db, api_key or creds.credentials)  # type: ignore[union-attr]
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthorized("Missing bearer token")
    claims = decode_jwt(creds.credentials, expected_type="access")
    try:
        user_id = uuid.UUID(claims["sub"])
        org_id = uuid.UUID(claims["org"])
    except (KeyError, ValueError) as e:
        raise Unauthorized("Malformed token") from e

    # Scope the session before the first query so every tenant-table read is filtered by RLS.
    await set_tenant(db, org_id)
    row = (
        await db.execute(
            select(User, Organization, Membership)
            .join(Membership, Membership.user_id == User.id)
            .join(Organization, Organization.id == Membership.org_id)
            .where(User.id == user_id, Organization.id == org_id)
        )
    ).first()
    if row is None or not row.User.is_active:
        raise Unauthorized("Membership not found")
    org_id_var.set(str(org_id))
    request.state.org_id = org_id
    return Principal(user=row.User, org=row.Organization, membership=row.Membership)


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_role(minimum: str):
    async def checker(principal: CurrentPrincipal) -> Principal:
        if not principal.has_role(minimum):
            raise Forbidden(f"Requires {minimum} role or higher")
        return principal

    return checker


AdminPrincipal = Annotated[Principal, Depends(require_role("admin"))]
ManagerPrincipal = Annotated[Principal, Depends(require_role("manager"))]

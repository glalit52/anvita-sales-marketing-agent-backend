import uuid
from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field

from app.schemas.common import Schema

Role = Literal["owner", "admin", "manager", "agent"]


class SignupIn(Schema):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    business_name: str = Field(min_length=1, max_length=200)
    industry: str | None = None
    timezone: str = "Asia/Kolkata"
    currency: str = Field(default="INR", min_length=3, max_length=3)


class LoginIn(Schema):
    email: EmailStr
    password: str
    org_id: uuid.UUID | None = None


class RefreshIn(Schema):
    refresh_token: str


class SwitchOrgIn(Schema):
    org_id: uuid.UUID


class AcceptInviteIn(Schema):
    token: str
    name: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, min_length=10, max_length=200)


class OrgSummary(Schema):
    id: uuid.UUID
    name: str
    slug: str
    role: Role


class UserOut(Schema):
    id: uuid.UUID
    name: str
    email: str
    created_at: datetime


class TokenPair(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class SessionOut(TokenPair):
    user: UserOut
    org: OrgSummary
    orgs: list[OrgSummary]


class MeOut(Schema):
    user: UserOut
    org: OrgSummary
    orgs: list[OrgSummary]

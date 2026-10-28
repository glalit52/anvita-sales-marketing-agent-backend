import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import Schema

# ---- Prospecting ------------------------------------------------------------------------------------


class PlanIn(Schema):
    prompt: str = Field(min_length=3, max_length=2000)


class PlanFilter(Schema):
    key: str
    label: str
    value: str


class SearchPlan(Schema):
    name: str = ""
    query: str = ""
    location: str = ""
    titles: list[str] = Field(default_factory=list)
    filters: list[PlanFilter] = Field(default_factory=list)
    min_rating: float = 0
    needs_website: bool = False
    sources: list[Literal["maps", "linkedin", "web"]] = Field(default_factory=lambda: ["maps", "linkedin"])
    limit: int = Field(default=50, ge=1, le=200)


class SearchIn(Schema):
    name: str = Field(max_length=200)
    prompt: str = Field(max_length=2000)
    plan: SearchPlan
    monitor: bool = False


class SearchOut(Schema):
    id: uuid.UUID
    name: str
    prompt: str
    plan: dict[str, Any]
    status: str
    monitor: bool
    row_count: int
    new_rows: int
    ai_columns: list[dict[str, Any]]
    credits_used: int
    error: str | None
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SearchPatch(Schema):
    name: str | None = Field(default=None, max_length=200)
    monitor: bool | None = None


class RowOut(Schema):
    id: uuid.UUID
    company: str
    city: str | None
    address: str | None
    rating: float | None
    reviews: int | None
    website: str | None
    employees: str | None
    category: str | None
    signals: list[str]
    contact_name: str | None
    role: str | None
    email: str | None
    email_status: str
    email_source: str | None
    phone: str | None
    phone_status: str
    phone_source: str | None
    linkedin_url: str | None
    fit: int
    sources: list[str]
    enriched: bool
    ai: dict[str, Any]
    contact_id: uuid.UUID | None


class RowsIn(Schema):
    row_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class AddToCrmIn(RowsIn):
    tag: str | None = Field(default=None, max_length=64)
    owner_id: uuid.UUID | None = None
    sequence_id: uuid.UUID | None = None


class AiColumnIn(Schema):
    question: str = Field(min_length=3, max_length=300)


# ---- Marketing --------------------------------------------------------------------------------------


class AdRecOut(Schema):
    id: uuid.UUID
    platform: str
    kind: str
    title: str
    rationale: str
    impact: str
    confidence: int
    campaign_ref: str | None
    change: dict[str, Any]
    status: str
    result: str | None
    decided_at: datetime | None
    created_at: datetime


class AdCopyIn(Schema):
    platform: Literal["google", "meta", "linkedin"] = "google"
    offer: str = Field(max_length=300)
    audience: str = Field(default="", max_length=300)


class ContentIn(Schema):
    title: str = Field(max_length=300)
    keyword: str = Field(max_length=200)
    type: Literal["Blog", "Service page", "FAQ", "Location page"] = "Blog"
    stage: Literal["Idea", "Brief", "Draft", "Review", "Published"] = "Idea"
    planned_for: datetime | None = None
    brief: str | None = None
    body: str | None = None
    meta_description: str | None = Field(default=None, max_length=300)
    published_url: str | None = None


class ContentOut(Schema):
    id: uuid.UUID
    title: str
    keyword: str
    type: str
    stage: str
    volume: int
    difficulty: int
    position: float | None
    gap: str | None
    brief: str | None
    body: str | None
    meta_description: str | None
    planned_for: datetime | None
    published_url: str | None
    published_at: datetime | None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SocialIn(Schema):
    networks: list[Literal["instagram", "facebook", "linkedin", "gbp"]] = Field(min_length=1)
    caption: str = Field(min_length=1, max_length=5000)
    media_urls: list[str] = Field(default_factory=list, max_length=10)
    scheduled_at: datetime | None = None
    ai: bool = False


class SocialOut(SocialIn):
    id: uuid.UUID
    status: str
    error: str | None
    published_at: datetime | None
    external_ids: dict[str, Any]
    created_at: datetime


class CaptionIn(Schema):
    network: str = "instagram"
    topic: str = Field(max_length=300)


class LandingIn(Schema):
    name: str = Field(max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,120}$")
    pipeline: Literal["clinic", "b2b"] = "clinic"
    campaign_id: uuid.UUID | None = None
    sections: list[dict[str, Any]] | None = None
    offer: str | None = Field(default=None, max_length=300)
    audience: str | None = Field(default=None, max_length=300)


class LandingPatch(Schema):
    name: str | None = Field(default=None, max_length=200)
    status: Literal["draft", "testing", "live"] | None = None
    sections: list[dict[str, Any]] | None = None


class LandingOut(Schema):
    id: uuid.UUID
    name: str
    slug: str
    url: str = ""
    pipeline: str
    status: str
    campaign_id: uuid.UUID | None
    campaign_name: str | None = None
    sections: list[dict[str, Any]]
    views: dict[str, Any]
    conversions: dict[str, Any]
    conversion: float = 0
    published_at: datetime | None
    updated_at: datetime


class RewriteIn(Schema):
    section_id: str
    instruction: str = Field(default="Make it clearer and more specific", max_length=300)


class AeoIn(Schema):
    prompt: str = Field(min_length=3, max_length=500)
    pipeline: Literal["clinic", "b2b"] = "clinic"
    volume: Literal["High", "Medium", "Low"] = "Medium"
    competitors: list[str] = Field(default_factory=list, max_length=10)


class AeoOut(AeoIn):
    id: uuid.UUID
    results: dict[str, Any]
    history: list[dict[str, Any]]
    score: int
    why: str | None
    actions: list[str]
    last_checked_at: datetime | None


class DgIn(Schema):
    name: str = Field(max_length=200)
    pipeline: Literal["clinic", "b2b"] = "clinic"
    goal: Literal["bookings", "demo", "webinar"] = "bookings"
    budget_minor: int = Field(default=0, ge=0)
    target: int = Field(default=0, ge=0)
    audience: list[str] = Field(default_factory=list)
    offer: str = Field(default="", max_length=500)
    starts_on: datetime | None = None
    ends_on: datetime | None = None


class DgOut(Schema):
    id: uuid.UUID
    name: str
    pipeline: str
    goal: str
    status: str
    owner_id: uuid.UUID | None
    starts_on: datetime | None
    ends_on: datetime | None
    budget_minor: int
    target: int
    audience: list[str]
    brief: str | None
    channels: list[dict[str, Any]]
    ai_summary: str | None
    utm_campaign: str | None
    results: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ApprovalOut(Schema):
    id: uuid.UUID
    kind: str
    channel: str
    pipeline: str
    title: str
    summary: str
    campaign_id: uuid.UUID | None
    campaign_name: str | None = None
    preview: dict[str, Any]
    diff: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    risk: str
    confidence: int
    impact: str | None
    status: str
    comment: str | None
    decided_by_id: uuid.UUID | None
    decided_by_name: str | None = None
    decided_at: datetime | None
    created_at: datetime


class DecisionIn(Schema):
    decision: Literal["approved", "rejected", "changes"]
    comment: str | None = Field(default=None, max_length=2000)


class MemoryIn(Schema):
    pipeline: Literal["clinic", "b2b"]
    memory: dict[str, list[str]]


# ---- Automations ------------------------------------------------------------------------------------


class WorkflowIn(Schema):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="Custom", max_length=40)
    enabled: bool = False
    trigger: dict[str, Any]
    nodes: list[dict[str, Any]] = Field(default_factory=list, max_length=60)


class WorkflowOut(WorkflowIn):
    id: uuid.UUID
    runs: int = 0
    success_rate: int = 100
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunOut(Schema):
    id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_name: str | None = None
    contact_id: uuid.UUID | None
    contact_name: str | None = None
    status: str
    steps_done: int
    total_steps: int = 0
    log: list[dict[str, Any]]
    error: str | None
    resume_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class CustomEventIn(Schema):
    event: str = Field(max_length=80)
    contact_id: uuid.UUID | None = None
    phone: str | None = None
    email: str | None = None
    name: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# ---- Agent studio -----------------------------------------------------------------------------------


class ScenarioIn(Schema):
    name: str = Field(max_length=200)
    channel: str = "whatsapp"
    messages: list[dict[str, str]] = Field(min_length=1, max_length=30)
    expect: dict[str, Any] = Field(default_factory=dict)


class ScenarioOut(ScenarioIn):
    id: uuid.UUID
    last_status: str | None
    last_reply: str | None
    last_detail: list[dict[str, Any]]
    last_run_at: datetime | None


class ChannelModeIn(Schema):
    modes: dict[str, Literal["off", "copilot", "autopilot"]]

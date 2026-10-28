import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import Schema

Channel = Literal["whatsapp", "sms", "email"]


class ReviewOut(Schema):
    id: uuid.UUID
    platform: str
    author: str
    rating: int
    text: str
    contact_id: uuid.UUID | None
    location_id: uuid.UUID | None
    sentiment: str | None
    topics: list[str]
    reply: str | None
    replied_at: datetime | None
    ai_draft: str | None
    ai_confidence: int | None
    flagged: str | None
    posted_at: datetime


class ReviewIn(Schema):
    platform: Literal["google", "facebook", "practo", "justdial", "internal"]
    author: str = Field(max_length=200)
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=5000)
    location_id: uuid.UUID | None = None
    posted_at: datetime | None = None


class ReplyIn(Schema):
    text: str = Field(min_length=1, max_length=4000)


class PlatformStat(Schema):
    platform: str
    rating: float
    count: int
    new_this_month: int
    response_rate: int


class ReviewStats(Schema):
    average: float
    total: int
    platforms: list[PlatformStat]
    distribution: list[dict[str, int]]
    trend: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    response_rate: int
    needs_reply: int


class ReviewRequestIn(Schema):
    contact_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    channel: Channel = "whatsapp"


class ReviewRequestOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str | None = None
    appointment_id: uuid.UUID | None
    channel: str
    status: str
    rating: int | None
    feedback: str | None
    issues: list[str]
    created_at: datetime


class ReviewRequestFunnel(Schema):
    sent: int
    clicked: int
    rated_high: int
    reviewed: int
    private_feedback: int


class ReviewSettings(Schema):
    auto_request: bool = True
    delay_hours: float = Field(default=2, ge=0, le=168)
    channel: Channel = "whatsapp"
    review_links: dict[str, str] = Field(default_factory=dict)


class SegmentCondition(Schema):
    field: str
    op: str = "eq"
    value: Any = None


class SegmentRules(Schema):
    match: Literal["all", "any"] = "all"
    conditions: list[SegmentCondition] = Field(default_factory=list)


class SegmentIn(Schema):
    name: str = Field(max_length=200)
    description: str | None = None
    rules: SegmentRules


class SegmentOut(SegmentIn):
    id: uuid.UUID
    count: int = 0


class SegmentPreview(Schema):
    count: int
    sample: list[dict[str, Any]]


class TemplateIn(Schema):
    name: str = Field(pattern=r"^[a-z0-9_]{1,120}$")
    channel: Channel
    category: Literal["Marketing", "Utility"] = "Marketing"
    language: str = "English"
    header: str | None = None
    subject: str | None = None
    body: str = Field(min_length=1, max_length=4096)
    buttons: list[str] = Field(default_factory=list, max_length=10)
    media: Literal["none", "image", "video"] = "none"
    provider_template_id: str | None = None


class TemplateOut(TemplateIn):
    id: uuid.UUID
    status: str
    rejection_reason: str | None


class TemplateStatusIn(Schema):
    status: Literal["approved", "rejected", "pending", "draft"]
    reason: str | None = None


class CampaignIn(Schema):
    name: str = Field(max_length=200)
    channel: Channel
    segment_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    subject: str | None = None
    body: str = ""
    scheduled_at: datetime | None = None


class CampaignOut(Schema):
    id: uuid.UUID
    name: str
    channel: str
    segment_id: uuid.UUID | None
    segment_name: str | None = None
    template_id: uuid.UUID | None
    template_name: str | None = None
    subject: str | None
    body: str
    status: str
    scheduled_at: datetime | None
    started_at: datetime | None
    sent_at: datetime | None
    audience: int
    skipped_no_consent: int
    stats: dict[str, int] = Field(default_factory=dict)
    created_at: datetime


class AudienceOut(Schema):
    eligible: int
    total: int


class StepIn(Schema):
    type: Literal["email", "whatsapp", "sms", "linkedin", "call"]
    day: int = Field(ge=0, le=365)
    title: str = Field(max_length=200)
    subject: str | None = None
    body: str = Field(max_length=10_000)
    ab_test: bool = False
    variant_b: str | None = None


class StepOut(StepIn):
    id: uuid.UUID
    position: int
    stats: dict[str, int] = Field(default_factory=dict)


class SequenceSettings(Schema):
    send_from: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    send_to: str = Field(default="18:00", pattern=r"^\d{2}:\d{2}$")
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])
    daily_cap: int = Field(default=60, ge=1, le=2000)
    stop_on_reply: bool = True


class SequenceIn(Schema):
    name: str = Field(max_length=200)
    audience: str | None = None
    owner_id: uuid.UUID | None = None
    settings: SequenceSettings = Field(default_factory=SequenceSettings)
    steps: list[StepIn] = Field(default_factory=list)


class SequenceOut(Schema):
    id: uuid.UUID
    name: str
    status: str
    owner_id: uuid.UUID | None
    audience: str | None
    settings: dict[str, Any]
    stats: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EnrollIn(Schema):
    contact_ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)


class EnrollmentOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str | None = None
    status: str
    next_position: int
    next_run_at: datetime | None
    reply_class: str | None
    replied_at: datetime | None
    created_at: datetime

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import EmailStr, Field, field_serializer

from app.schemas.common import Schema

Stage = Literal["prospect", "new", "qualified", "booked", "customer", "lost"]
DealStage = Literal["new", "qualified", "booked", "proposal", "won", "lost"]
ChannelType = Literal["whatsapp", "sms", "email", "webchat", "instagram", "facebook", "google", "voice", "linkedin"]
ConsentStatus = Literal["opted_in", "opted_out", "unknown"]
TaskType = Literal["call", "whatsapp", "email", "review", "payment"]
Priority = Literal["high", "normal", "low"]


class ContactIn(Schema):
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    company: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    stage: Stage = "new"
    source: str | None = Field(default=None, max_length=80)
    channel: ChannelType | None = None
    tags: list[str] = Field(default_factory=list, max_length=50)
    # None = owned by the AI agent.
    owner_id: uuid.UUID | None = None
    score: int = Field(default=0, ge=0, le=100)
    value_minor: int = Field(default=0, ge=0)
    language: str | None = Field(default=None, max_length=40)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ContactUpdate(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    company: str | None = None
    city: str | None = None
    stage: Stage | None = None
    source: str | None = None
    channel: ChannelType | None = None
    tags: list[str] | None = None
    owner_id: uuid.UUID | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    value_minor: int | None = Field(default=None, ge=0)
    language: str | None = None
    attributes: dict[str, Any] | None = None


class ContactOut(Schema):
    id: uuid.UUID
    name: str
    phone: str | None
    email: str | None
    company: str | None
    city: str | None
    stage: Stage
    source: str | None
    channel: str | None
    tags: list[str]
    owner_id: uuid.UUID | None
    score: int
    value_minor: int
    language: str | None
    attributes: dict[str, Any]
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("owner_id")
    def _owner(self, v: uuid.UUID | None) -> str:
        # The web app models AI ownership as the literal "ai".
        return str(v) if v else "ai"


class BulkContactAction(Schema):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    stage: Stage | None = None
    owner_id: uuid.UUID | None = None
    assign_to_ai: bool = False


class ConsentIn(Schema):
    channel: Literal["whatsapp", "sms", "email"]
    purpose: Literal["marketing", "transactional"] = "marketing"
    status: ConsentStatus
    source: str = Field(max_length=120)


class ConsentOut(ConsentIn):
    captured_at: datetime


class NoteIn(Schema):
    body: str = Field(min_length=1, max_length=10_000)
    pinned: bool = False


class NoteOut(Schema):
    id: uuid.UUID
    body: str
    pinned: bool
    author_id: uuid.UUID | None
    created_at: datetime


class TimelineEvent(Schema):
    id: str
    kind: Literal["message", "ai", "booking", "payment", "note", "task", "stage"]
    title: str
    detail: str | None = None
    at: datetime
    channel: str | None = None
    actor: str | None = None
    meta: str | None = None


class DealIn(Schema):
    contact_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    pipeline: str = Field(default="default", max_length=32)
    stage: DealStage = "new"
    value_minor: int = Field(default=0, ge=0)
    probability: int | None = Field(default=None, ge=0, le=100)
    owner_id: uuid.UUID | None = None
    expected_close: date | None = None
    notes: str | None = None


class DealUpdate(Schema):
    title: str | None = Field(default=None, max_length=200)
    stage: DealStage | None = None
    value_minor: int | None = Field(default=None, ge=0)
    probability: int | None = Field(default=None, ge=0, le=100)
    owner_id: uuid.UUID | None = None
    expected_close: date | None = None
    lost_reason: str | None = None
    notes: str | None = None


class DealOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    title: str
    pipeline: str
    stage: DealStage
    value_minor: int
    probability: int
    owner_id: uuid.UUID | None
    expected_close: date | None
    lost_reason: str | None
    closed_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class PipelineStageSummary(Schema):
    stage: DealStage
    count: int
    value_minor: int
    weighted_minor: int


class TaskIn(Schema):
    title: str = Field(min_length=1, max_length=300)
    type: TaskType = "call"
    contact_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    due_at: datetime
    has_time: bool = True
    priority: Priority = "normal"
    notes: str | None = None


class TaskUpdate(Schema):
    title: str | None = Field(default=None, max_length=300)
    type: TaskType | None = None
    assignee_id: uuid.UUID | None = None
    due_at: datetime | None = None
    has_time: bool | None = None
    priority: Priority | None = None
    done: bool | None = None
    notes: str | None = None


class TaskOut(Schema):
    id: uuid.UUID
    title: str
    type: TaskType
    contact_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    created_by_type: str
    created_by_id: uuid.UUID | None
    created_at: datetime
    due_at: datetime
    has_time: bool
    priority: Priority
    done: bool
    done_at: datetime | None
    ai_context: str | None
    ai_confidence: int | None
    notes: str | None

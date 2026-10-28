"""Reviews, review requests, segments, templates, campaigns and sequences."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Review(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        Index(
            "uq_reviews_org_platform_external",
            "org_id",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("ix_reviews_org_posted", "org_id", "posted_at"),
    )

    platform: Mapped[str] = mapped_column(String(16))  # google | facebook | practo | justdial | internal
    external_id: Mapped[str | None] = mapped_column(String(200))
    author: Mapped[str] = mapped_column(String(200))
    rating: Mapped[int]
    text: Mapped[str] = mapped_column(Text, default="")
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    sentiment: Mapped[str | None] = mapped_column(String(16))
    topics: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    reply: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None]
    ai_draft: Mapped[str | None] = mapped_column(Text)
    ai_confidence: Mapped[int | None]
    flagged: Mapped[str | None] = mapped_column(String(300))
    posted_at: Mapped[datetime]


class ReviewRequest(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "review_requests"

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16))
    token: Mapped[str] = mapped_column(String(40), unique=True)
    # sent → clicked → rated → reviewed (4–5★ posted publicly) | private_feedback (1–3★)
    status: Mapped[str] = mapped_column(String(20), default="sent")
    rating: Mapped[int | None]
    feedback: Mapped[str | None] = mapped_column(Text)
    issues: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)
    suggestions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    clicked_at: Mapped[datetime | None]
    rated_at: Mapped[datetime | None]


class Segment(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "segments"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(500))
    # {"match": "all"|"any", "conditions": [{"field": "stage", "op": "in", "value": ["customer"]}, ...]}
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class MessageTemplate(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "message_templates"
    __table_args__ = (UniqueConstraint("org_id", "channel", "name"),)

    name: Mapped[str] = mapped_column(String(120))
    channel: Mapped[str] = mapped_column(String(16))  # whatsapp | sms | email
    category: Mapped[str] = mapped_column(String(16), default="Marketing")  # Marketing | Utility
    language: Mapped[str] = mapped_column(String(40), default="English")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | pending | approved | rejected
    header: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    media: Mapped[str] = mapped_column(String(8), default="none")
    # WhatsApp: the approved template name/id at Meta. SMS in India: the DLT template id.
    provider_template_id: Mapped[str | None] = mapped_column(String(120))
    rejection_reason: Mapped[str | None] = mapped_column(String(300))


class Campaign(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(16))
    segment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("segments.id", ondelete="SET NULL"))
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message_templates.id", ondelete="SET NULL"))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | scheduled | sending | sent | cancelled
    scheduled_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    audience_count: Mapped[int] = mapped_column(default=0)
    skipped_no_consent: Mapped[int] = mapped_column(default=0)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CampaignRecipient(IdMixin, TenantMixin, Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id"),)

    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | sent | failed | skipped
    error: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime | None]


class Sequence(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "sequences"

    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | active | paused
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    audience: Mapped[str | None] = mapped_column(String(300))
    # {"send_from": "09:00", "send_to": "18:00", "days": [0..5], "daily_cap": 60, "stop_on_reply": true}
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class SequenceStep(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "sequence_steps"

    sequence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sequences.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    type: Mapped[str] = mapped_column(String(16))  # email | whatsapp | sms | linkedin | call
    day: Mapped[int] = mapped_column(default=0)  # days after enrollment
    title: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    ab_test: Mapped[bool] = mapped_column(default=False)
    variant_b: Mapped[str | None] = mapped_column(Text)


class Enrollment(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        Index(
            "uq_enrollments_active",
            "sequence_id",
            "contact_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    sequence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sequences.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    # active | completed | replied | stopped | bounced | unsubscribed
    status: Mapped[str] = mapped_column(String(16), default="active")
    next_position: Mapped[int] = mapped_column(default=0)
    next_run_at: Mapped[datetime | None]
    stop_reason: Mapped[str | None] = mapped_column(String(200))
    reply_class: Mapped[str | None] = mapped_column(String(24))
    replied_at: Mapped[datetime | None]
    # AI personalisation values computed once per contact ({{ai_first_line}}, {{ai_pain_point}}).
    vars: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    enrolled_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class SequenceSend(IdMixin, TenantMixin, Base):
    __tablename__ = "sequence_sends"
    __table_args__ = (UniqueConstraint("enrollment_id", "step_id"),)

    enrollment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sequence_steps.id", ondelete="CASCADE"), index=True)
    variant: Mapped[str] = mapped_column(String(1), default="a")
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | task_created | failed | skipped
    error: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime]

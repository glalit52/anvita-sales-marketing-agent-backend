"""Prospecting, marketing suite (ads, content, social, landing pages, AEO, demand-gen campaigns and
approvals), automations and agent scenario tests."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

# ---- Prospecting ------------------------------------------------------------------------------------


class ProspectSearch(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "prospect_searches"

    name: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    # {"query", "location", "titles": [...], "keywords", "filters": [{key,label,value}], "sources": [...], "limit"}
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | running | ready | failed
    monitor: Mapped[bool] = mapped_column(default=False)
    row_count: Mapped[int] = mapped_column(default=0)
    new_rows: Mapped[int] = mapped_column(default=0)
    # [{"id": "c1", "question": "Do they sell drip irrigation?"}]
    ai_columns: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    credits_used: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    last_run_at: Mapped[datetime | None]
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ProspectRow(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "prospect_rows"
    __table_args__ = (UniqueConstraint("search_id", "dedupe_key"),)

    search_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prospect_searches.id", ondelete="CASCADE"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(500))
    rating: Mapped[float | None] = mapped_column(Float)
    reviews: Mapped[int | None]
    website: Mapped[str | None] = mapped_column(String(300))
    employees: Mapped[str | None] = mapped_column(String(40))
    category: Mapped[str | None] = mapped_column(String(200))
    signals: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    contact_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    email_status: Mapped[str] = mapped_column(String(16), default="not_found")
    email_source: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    phone_status: Mapped[str] = mapped_column(String(16), default="not_found")
    phone_source: Mapped[str | None] = mapped_column(String(200))
    linkedin_url: Mapped[str | None] = mapped_column(String(300))
    fit: Mapped[int] = mapped_column(default=0)
    sources: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    enriched: Mapped[bool] = mapped_column(default=False)
    # {"c1": {"value": "Yes", "note": "...", "confidence": 90}}
    ai: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))


# ---- Marketing --------------------------------------------------------------------------------------


class AdRecommendation(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "ad_recommendations"

    platform: Mapped[str] = mapped_column(String(16))  # google | meta | linkedin
    kind: Mapped[str] = mapped_column(String(16))  # budget | pause | copy | keyword | schedule
    title: Mapped[str] = mapped_column(String(300))
    rationale: Mapped[str] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(String(200))
    confidence: Mapped[int] = mapped_column(default=70)
    campaign_ref: Mapped[str | None] = mapped_column(String(120))
    change: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # sent to AdsProvider.apply()
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | applied | dismissed | failed
    result: Mapped[str | None] = mapped_column(String(500))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None]


class ContentItem(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "content_items"

    title: Mapped[str] = mapped_column(String(300))
    keyword: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(32), default="Blog")  # Blog | Service page | FAQ | Location page
    stage: Mapped[str] = mapped_column(String(16), default="Idea")  # Idea | Brief | Draft | Review | Published
    volume: Mapped[int] = mapped_column(default=0)
    difficulty: Mapped[int] = mapped_column(default=0)
    position: Mapped[float | None] = mapped_column(Float)
    gap: Mapped[str | None] = mapped_column(String(500))
    brief: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)  # markdown
    meta_description: Mapped[str | None] = mapped_column(String(300))
    planned_for: Mapped[datetime | None]
    published_url: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None]


class SocialPost(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "social_posts"
    __table_args__ = (Index("ix_social_posts_org_scheduled", "org_id", "scheduled_at"),)

    networks: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)
    caption: Mapped[str] = mapped_column(Text)
    media_urls: Mapped[list[str]] = mapped_column(ARRAY(String(1000)), default=list)
    scheduled_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | scheduled | published | failed
    ai: Mapped[bool] = mapped_column(default=False)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None]


class LandingPage(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "landing_pages"
    __table_args__ = (UniqueConstraint("org_id", "slug"),)

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    pipeline: Mapped[str] = mapped_column(String(16), default="clinic")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | testing | live
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dg_campaigns.id", ondelete="SET NULL"))
    # [{"id": "hero", "label", "enabled", "variants": {"A": {...}, "B": {...}}, "rewrites": []}]
    sections: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    views: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # {"A": 0, "B": 0}
    conversions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    published_at: Mapped[datetime | None]


class AeoPrompt(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "aeo_prompts"

    prompt: Mapped[str] = mapped_column(String(500))
    pipeline: Mapped[str] = mapped_column(String(16), default="clinic")
    volume: Mapped[str] = mapped_column(String(8), default="Medium")
    competitors: Mapped[list[str]] = mapped_column(ARRAY(String(200)), default=list)
    # {"chatgpt": {"mentioned", "cited", "rank", "answer", "citedUrls"}}
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)  # [{"at", "score"}]
    score: Mapped[int] = mapped_column(default=0)
    why: Mapped[str | None] = mapped_column(Text)
    actions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    last_checked_at: Mapped[datetime | None]


class DgCampaign(IdMixin, TenantMixin, TimestampMixin, Base):
    """A multi-channel demand-gen campaign planned by the AI and approved by the team."""

    __tablename__ = "dg_campaigns"

    name: Mapped[str] = mapped_column(String(200))
    pipeline: Mapped[str] = mapped_column(String(16), default="clinic")  # clinic | b2b
    goal: Mapped[str] = mapped_column(String(16), default="bookings")  # bookings | demo | webinar
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned | active | completed
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    starts_on: Mapped[datetime | None]
    ends_on: Mapped[datetime | None]
    budget_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    target: Mapped[int] = mapped_column(default=0)
    audience: Mapped[list[str]] = mapped_column(JSONB, default=list)
    brief: Mapped[str | None] = mapped_column(Text)
    # [{"channel", "status", "budgetMinor", "spendMinor", "leads", "outcomes", "assets": [...], "note"}]
    channels: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    utm_campaign: Mapped[str | None] = mapped_column(String(120))


class Approval(IdMixin, TenantMixin, TimestampMixin, Base):
    """Anything the AI wants to publish or spend waits here until a human approves it."""

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_org_status", "org_id", "status"),)

    kind: Mapped[str] = mapped_column(String(16))  # ad | email | landing | budget | aeo | whatsapp | social
    channel: Mapped[str] = mapped_column(String(16))
    pipeline: Mapped[str] = mapped_column(String(16), default="clinic")
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dg_campaigns.id", ondelete="CASCADE"))
    # What happens on approval: {"type": "publish_landing" | "apply_ad" | "publish_social" | ..., ...}
    action: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    risk: Mapped[str] = mapped_column(String(8), default="low")
    confidence: Mapped[int] = mapped_column(default=70)
    impact: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected | changes
    comment: Mapped[str | None] = mapped_column(Text)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None]


# ---- Automations ------------------------------------------------------------------------------------


class Workflow(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(40), default="Custom")
    enabled: Mapped[bool] = mapped_column(default=False)
    # trigger: {"type": "message" | "form" | "booked" | "completed" | "payment" | "tag" | "review" | ..., "config": {}}
    trigger: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # [{"id", "kind": "condition|delay|action|ai", "type", "title", "detail", "config": {}, "no": [...]}]
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)


class WorkflowRun(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (Index("ix_workflow_runs_org_created", "org_id", "created_at"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    trigger_event_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|waiting|success|branched|failed
    # Where to continue: path into the node tree, e.g. [3] or [1, "no", 0]
    cursor: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    vars: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    steps_done: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    resume_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


# ---- Agent studio -----------------------------------------------------------------------------------


class ScenarioTest(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "scenario_tests"

    name: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(16), default="whatsapp")
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)  # [{"role", "body"}]
    # {"mustInclude": [...], "mustNotInclude": [...], "tool": "book_appointment", "escalate": bool}
    expect: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_status: Mapped[str | None] = mapped_column(String(8))  # pass | fail
    last_reply: Mapped[str | None] = mapped_column(Text)
    last_detail: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    last_run_at: Mapped[datetime | None]

"""The integrations catalog shown in Settings → Integrations, and how each one connects.

auth:
  builtin  – configured elsewhere in Anvita (channels, webhooks, payments env); status is derived
  api_key  – the workspace pastes credentials (the provider's env var names are the form fields)
  oauth    – needs the platform's OAuth app for that vendor; until it exists, connect returns 501 with the
             env vars to set
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Channel, IntegrationConnection, WebhookEndpoint
from app.services import providers


@dataclass(frozen=True)
class Integration:
    id: str
    name: str
    category: str
    description: str
    color: str
    permissions: tuple[str, ...]
    auth: str  # builtin | api_key | oauth
    provider: str | None = None  # provider spec id it powers (credentials map to its env vars)
    popular: bool = False


CATALOG: list[Integration] = [
    Integration(
        "whatsapp",
        "WhatsApp Business",
        "Messaging",
        "Official Cloud API: templates, catalogs, click-to-WhatsApp ads",
        "#25d366",
        ("Send & receive messages", "Manage message templates", "Read phone number quality rating"),
        "builtin",
        popular=True,
    ),
    Integration(
        "twilio",
        "Twilio",
        "Messaging",
        "SMS and voice numbers worldwide",
        "#f22f46",
        ("Send & receive SMS", "Receive call webhooks", "Buy numbers"),
        "api_key",
        "twilio",
    ),
    Integration(
        "exotel",
        "Exotel",
        "Messaging",
        "Indian cloud telephony: virtual numbers, missed calls, IVR",
        "#1f6feb",
        ("Receive call events", "Call recordings", "Click-to-call"),
        "api_key",
        "exotel",
        popular=True,
    ),
    Integration(
        "smtp",
        "Email (SMTP)",
        "Messaging",
        "Send sequences and replies from your own mailbox",
        "#ea4335",
        ("Send email",),
        "api_key",
        "smtp",
    ),
    Integration(
        "sendgrid",
        "SendGrid",
        "Messaging",
        "High-volume email sending",
        "#1a82e2",
        ("Send email",),
        "api_key",
        "sendgrid",
    ),
    Integration(
        "hubspot",
        "HubSpot",
        "CRM",
        "Two-way sync of contacts, deals and activities",
        "#ff7a59",
        ("Read & write contacts", "Read & write deals", "Log activities"),
        "api_key",
        "hubspot",
        popular=True,
    ),
    Integration(
        "salesforce",
        "Salesforce",
        "CRM",
        "Leads, opportunities and tasks",
        "#00a1e0",
        ("Read & write leads", "Read & write opportunities", "Create tasks"),
        "oauth",
    ),
    Integration(
        "zoho",
        "Zoho CRM",
        "CRM",
        "Popular with Indian SMBs: leads, deals, modules",
        "#e42527",
        ("Read & write leads", "Read & write deals"),
        "oauth",
        popular=True,
    ),
    Integration(
        "gcal",
        "Google Calendar",
        "Calendar",
        "Real-time availability and booking for each chair/doctor",
        "#4285f4",
        ("Read free/busy", "Create & update events"),
        "oauth",
        popular=True,
    ),
    Integration(
        "razorpay",
        "Razorpay",
        "Payments",
        "UPI, cards and netbanking pay links",
        "#0c2451",
        ("Create payment links", "Read payments", "Issue refunds (human only)"),
        "builtin",
        popular=True,
    ),
    Integration(
        "stripe",
        "Stripe",
        "Payments",
        "International cards and subscriptions",
        "#635bff",
        ("Create checkout sessions", "Read payments"),
        "oauth",
    ),
    Integration(
        "gads",
        "Google Ads",
        "Marketing",
        "Ads co-pilot, offline conversion upload",
        "#fbbc04",
        ("Read campaigns & spend", "Upload offline conversions", "Edit budgets (with approval)"),
        "api_key",
        "google_ads",
    ),
    Integration(
        "meta",
        "Meta Ads",
        "Marketing",
        "Facebook & Instagram ads, lead forms, CAPI",
        "#0866ff",
        ("Read campaigns", "Receive lead forms", "Send Conversions API events"),
        "api_key",
        "meta_ads",
    ),
    Integration(
        "meta_social",
        "Facebook & Instagram pages",
        "Marketing",
        "Schedule and publish posts",
        "#e1306c",
        ("Publish posts",),
        "api_key",
        "meta_social",
    ),
    Integration(
        "linkedin",
        "LinkedIn",
        "Marketing",
        "Publish company page posts",
        "#0a66c2",
        ("Publish posts",),
        "api_key",
        "linkedin_social",
    ),
    Integration(
        "gsc",
        "Search Console",
        "Marketing",
        "SEO queries, pages and gaps",
        "#458cf5",
        ("Read search analytics",),
        "api_key",
        "gsc",
    ),
    Integration(
        "gbp",
        "Google Business Profile",
        "Marketing",
        "Reviews, Q&A, posts and chat for every location",
        "#34a853",
        ("Read & reply to reviews", "Manage posts", "Read insights"),
        "api_key",
        "gbp",
        popular=True,
    ),
    Integration(
        "aeo",
        "Answer engines (AEO)",
        "Marketing",
        "Track how ChatGPT, Perplexity and Gemini mention you",
        "#10a37f",
        ("Ask tracked prompts",),
        "api_key",
        "perplexity",
    ),
    Integration(
        "apollo",
        "Apollo.io",
        "Data",
        "People search and contact enrichment for prospecting",
        "#1d4ed8",
        ("Search people", "Enrich emails & phones"),
        "api_key",
        "apollo",
        popular=True,
    ),
    Integration(
        "google_places",
        "Google Places",
        "Data",
        "Find local businesses for prospecting",
        "#4285f4",
        ("Search places",),
        "api_key",
        "google_places",
    ),
    Integration(
        "hunter", "Hunter.io", "Data", "Find and verify work emails", "#fa5320", ("Find emails",), "api_key", "hunter"
    ),
    Integration(
        "serpapi",
        "SerpAPI",
        "Data",
        "Web search results for prospect research",
        "#2b2d42",
        ("Search the web",),
        "api_key",
        "serpapi",
    ),
    Integration(
        "wordpress",
        "WordPress",
        "Website & commerce",
        "Publish SEO posts, install webchat",
        "#21759b",
        ("Create & publish posts", "Install plugin"),
        "oauth",
    ),
    Integration(
        "shopify",
        "Shopify",
        "Website & commerce",
        "Orders, abandoned carts, customers",
        "#5e8e3e",
        ("Read orders", "Read customers", "Read checkouts"),
        "oauth",
    ),
    Integration(
        "zapier",
        "Zapier",
        "Automation",
        "Connect 6,000+ apps with Anvita triggers",
        "#ff4f00",
        ("Use Anvita triggers & actions",),
        "builtin",
    ),
    Integration(
        "webhooks",
        "Webhooks",
        "Automation",
        "Push events to your own endpoints",
        "#475569",
        ("Send signed event payloads",),
        "builtin",
    ),
]
BY_ID = {i.id: i for i in CATALOG}
OAUTH_ENV = {
    "salesforce": ["ANVITA_SALESFORCE_CLIENT_ID", "ANVITA_SALESFORCE_CLIENT_SECRET"],
    "zoho": ["ANVITA_ZOHO_CLIENT_ID", "ANVITA_ZOHO_CLIENT_SECRET"],
    "gcal": ["ANVITA_GOOGLE_OAUTH_CLIENT_ID", "ANVITA_GOOGLE_OAUTH_CLIENT_SECRET"],
    "stripe": ["ANVITA_STRIPE_CONNECT_CLIENT_ID"],
    "wordpress": ["ANVITA_WORDPRESS_CLIENT_ID", "ANVITA_WORDPRESS_CLIENT_SECRET"],
    "shopify": ["ANVITA_SHOPIFY_API_KEY", "ANVITA_SHOPIFY_API_SECRET"],
}


def fields_for(integration: Integration) -> list[str]:
    if integration.provider:
        spec = next((s for s in providers.specs() if s.id == integration.provider), None)
        return list(spec.env) if spec else []
    return []


async def org_credentials(db: AsyncSession, org_id: uuid.UUID, provider_id: str) -> dict[str, str]:
    """Credentials a workspace stored for the integration that powers this provider."""
    integration = next((i for i in CATALOG if i.provider == provider_id), None)
    if integration is None:
        return {}
    conn = await db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.org_id == org_id,
            IntegrationConnection.integration_id == integration.id,
            IntegrationConnection.status == "connected",
        )
    )
    return {k: str(v) for k, v in (conn.credentials if conn else {}).items()}


async def resolve_for_org(db: AsyncSession, org_id: uuid.UUID, kind: str) -> Any:
    """Provider of a kind using the workspace's own credentials first, then platform env, then dev samples."""
    overrides: dict[str, str] = {}
    for spec in providers.specs(kind):
        overrides.update(await org_credentials(db, org_id, spec.id))
    return providers.resolve(kind, overrides=overrides)


async def _overrides(db: AsyncSession, org_id: uuid.UUID, kind: str) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for spec in providers.specs(kind):
        overrides.update(await org_credentials(db, org_id, spec.id))
    return overrides


async def resolve_all_for_org(db: AsyncSession, org_id: uuid.UUID, kind: str) -> list[Any]:
    return providers.resolve_all(kind, overrides=await _overrides(db, org_id, kind))


async def try_resolve_for_org(db: AsyncSession, org_id: uuid.UUID, kind: str) -> Any | None:
    """Like resolve_for_org, but None instead of 501 for optional data sources."""
    try:
        return await resolve_for_org(db, org_id, kind)
    except providers.ProviderNotConfigured:
        return None


async def catalog_status(db: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    conns = {
        c.integration_id: c
        for c in (await db.scalars(select(IntegrationConnection).where(IntegrationConnection.org_id == org_id))).all()
    }
    wa_channels = (
        await db.scalar(
            select(func.count())
            .select_from(Channel)
            .where(Channel.org_id == org_id, Channel.type == "whatsapp", Channel.status == "connected")
        )
        or 0
    )
    endpoints = (
        await db.scalar(
            select(func.count())
            .select_from(WebhookEndpoint)
            .where(WebhookEndpoint.org_id == org_id, WebhookEndpoint.active.is_(True))
        )
        or 0
    )
    out = []
    for i in CATALOG:
        conn = conns.get(i.id)
        connected, account, health, last_sync, error = False, None, None, None, None
        if i.id == "whatsapp":
            connected, account = wa_channels > 0, f"{wa_channels} number{'s' if wa_channels != 1 else ''}"
        elif i.id == "webhooks":
            connected, account = endpoints > 0, f"{endpoints} endpoint{'s' if endpoints != 1 else ''}"
        elif i.id == "razorpay":
            connected = get_settings().payments_provider == "razorpay"
            account = "Configured by your Anvita admin" if connected else None
        elif i.id == "zapier":
            connected = False
        elif conn and conn.status != "disconnected":
            connected, account, last_sync, error = True, conn.account, conn.last_sync_at, conn.last_error
            health = "error" if conn.status == "error" else "healthy"
        spec_configured = None
        if i.provider:
            spec = next((s for s in providers.specs() if s.id == i.provider), None)
            spec_configured = bool(spec and spec.credentials())
        out.append(
            {
                "id": i.id,
                "name": i.name,
                "category": i.category,
                "description": i.description,
                "color": i.color,
                "permissions": list(i.permissions),
                "popular": i.popular,
                "auth": i.auth,
                "connected": connected,
                "account": account,
                "health": health if connected else None,
                "lastSyncAt": last_sync,
                "lastError": error,
                "fields": fields_for(i),
                # Provided at platform level (env), so every workspace can use it without its own keys.
                "platformProvided": spec_configured,
                "oauthEnv": OAUTH_ENV.get(i.id, []),
            }
        )
    return out

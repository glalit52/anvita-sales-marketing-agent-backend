"""Settings → integrations, API keys, webhooks, billing, compliance and notification preferences."""

import secrets
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import DB, AdminPrincipal, CurrentPrincipal
from app.core.config import get_settings
from app.core.errors import AppError, Forbidden, NotFound
from app.core.security import sha256
from app.db.base import utcnow
from app.models import ApiKey, BillingInvoice, CreditLedger, DataRequest, IntegrationConnection, WebhookEndpoint
from app.schemas.common import Ok
from app.schemas.platform import (
    API_SCOPES,
    ApiKeyIn,
    ApiKeyOut,
    BillingOut,
    BuyCreditsIn,
    ChangePlanIn,
    CheckoutOut,
    ConnectIn,
    CreditPackOut,
    DataRequestIn,
    DataRequestOut,
    IntegrationOut,
    InvoiceOut,
    MeterOut,
    NotificationPrefs,
    PlanOut,
    ProviderStatusOut,
    WebhookEndpointIn,
    WebhookEndpointOut,
)
from app.services import billing, compliance, providers
from app.services.audit import audit
from app.services.events import emit
from app.services.integrations import BY_ID, OAUTH_ENV, catalog_status, fields_for
from app.services.webhooks_out import PUBLIC_EVENTS

router = APIRouter(tags=["platform"])


# ---- Integrations ----------------------------------------------------------------------------------


@router.get("/integrations", response_model=list[IntegrationOut])
async def list_integrations(principal: CurrentPrincipal, db: DB) -> list[IntegrationOut]:
    return [IntegrationOut.model_validate(i) for i in await catalog_status(db, principal.org_id)]


@router.post("/integrations/{integration_id}/connect", response_model=IntegrationOut)
async def connect(integration_id: str, body: ConnectIn, principal: AdminPrincipal, db: DB) -> IntegrationOut:
    integration = BY_ID.get(integration_id)
    if integration is None:
        raise NotFound("Unknown integration")
    if integration.auth == "builtin":
        raise AppError(f"{integration.name} is set up from its own settings page", code="builtin_integration")
    if integration.auth == "oauth":
        raise providers.ProviderNotConfigured(
            f"{integration.name} needs the platform's OAuth app before workspaces can connect",
            details={"integration": integration_id, "env": OAUTH_ENV.get(integration_id, [])},
        )
    fields = fields_for(integration)
    missing = [f for f in fields if not body.credentials.get(f)]
    spec_ok = next((s for s in providers.specs() if s.id == integration.provider), None)
    if missing and not (spec_ok and spec_ok.credentials()):
        raise AppError("Missing credentials", code="missing_credentials", details={"missing": missing})
    conn = await db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.org_id == principal.org_id, IntegrationConnection.integration_id == integration_id
        )
    )
    if conn is None:
        conn = IntegrationConnection(org_id=principal.org_id, integration_id=integration_id)
        db.add(conn)
    conn.status, conn.account, conn.config = "connected", body.account, body.config
    conn.credentials = {k: v for k, v in body.credentials.items() if k in fields}
    conn.connected_by_id, conn.last_sync_at, conn.last_error = principal.user_id, utcnow(), None
    audit(
        db, principal.org_id, "integration.connected", actor_id=principal.user_id, data={"integration": integration_id}
    )
    await db.commit()
    return next(
        IntegrationOut.model_validate(i)
        for i in await catalog_status(db, principal.org_id)
        if i["id"] == integration_id
    )


@router.post("/integrations/{integration_id}/disconnect", response_model=Ok)
async def disconnect(integration_id: str, principal: AdminPrincipal, db: DB) -> Ok:
    conn = await db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.org_id == principal.org_id, IntegrationConnection.integration_id == integration_id
        )
    )
    if conn is None:
        raise NotFound("Not connected")
    conn.status, conn.credentials = "disconnected", {}
    audit(
        db,
        principal.org_id,
        "integration.disconnected",
        actor_id=principal.user_id,
        data={"integration": integration_id},
    )
    await db.commit()
    return Ok()


@router.get("/platform/providers", response_model=list[ProviderStatusOut])
async def provider_list(principal: AdminPrincipal) -> list[ProviderStatusOut]:
    """Which third-party slots the platform has keys for (developer view)."""
    return [ProviderStatusOut.model_validate(p) for p in providers.provider_status()]


# ---- API keys ---------------------------------------------------------------------------------------


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_keys(principal: AdminPrincipal, db: DB) -> list[ApiKeyOut]:
    rows = await db.scalars(
        select(ApiKey)
        .where(ApiKey.org_id == principal.org_id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    )
    return [ApiKeyOut.model_validate(r) for r in rows]


@router.get("/api-keys/scopes", response_model=list[str])
async def list_scopes(principal: CurrentPrincipal) -> list[str]:
    return API_SCOPES


@router.post("/api-keys", response_model=ApiKeyOut, status_code=201)
async def create_key(body: ApiKeyIn, principal: AdminPrincipal, db: DB) -> ApiKeyOut:
    bad = [s for s in body.scopes if s not in API_SCOPES]
    if bad:
        raise AppError("Unknown scopes", code="invalid_scopes", details={"scopes": bad})
    raw = f"anv_live_{secrets.token_urlsafe(32)}"
    key = ApiKey(
        org_id=principal.org_id,
        name=body.name,
        prefix=raw[:13],
        key_hash=sha256(raw),
        scopes=body.scopes,
        created_by_id=principal.user_id,
    )
    db.add(key)
    audit(db, principal.org_id, "api_key.created", actor_id=principal.user_id, data={"name": body.name})
    await db.commit()
    out = ApiKeyOut.model_validate(key)
    out.key = raw
    return out


@router.delete("/api-keys/{key_id}", response_model=Ok)
async def revoke_key(key_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> Ok:
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise NotFound("API key not found")
    key.revoked_at = utcnow()
    audit(db, principal.org_id, "api_key.revoked", actor_id=principal.user_id, data={"name": key.name})
    await db.commit()
    return Ok()


# ---- Webhook endpoints ------------------------------------------------------------------------------


@router.get("/webhook-events", response_model=list[str])
async def webhook_events(principal: CurrentPrincipal) -> list[str]:
    return sorted(set(PUBLIC_EVENTS.values()))


@router.get("/webhook-endpoints", response_model=list[WebhookEndpointOut])
async def list_endpoints(principal: AdminPrincipal, db: DB) -> list[WebhookEndpointOut]:
    rows = await db.scalars(
        select(WebhookEndpoint).where(WebhookEndpoint.org_id == principal.org_id).order_by(WebhookEndpoint.created_at)
    )
    return [WebhookEndpointOut.model_validate(r) for r in rows]


@router.post("/webhook-endpoints", response_model=WebhookEndpointOut, status_code=201)
async def create_endpoint(body: WebhookEndpointIn, principal: AdminPrincipal, db: DB) -> WebhookEndpointOut:
    valid = set(PUBLIC_EVENTS.values())
    if bad := [e for e in body.events if e not in valid]:
        raise AppError("Unknown events", code="invalid_events", details={"events": bad})
    ep = WebhookEndpoint(
        org_id=principal.org_id,
        url=str(body.url),
        events=body.events,
        active=body.active,
        secret=f"whsec_{secrets.token_urlsafe(24)}",
    )
    db.add(ep)
    await db.commit()
    out = WebhookEndpointOut.model_validate(ep)
    out.secret = ep.secret
    return out


@router.put("/webhook-endpoints/{endpoint_id}", response_model=WebhookEndpointOut)
async def update_endpoint(
    endpoint_id: uuid.UUID, body: WebhookEndpointIn, principal: AdminPrincipal, db: DB
) -> WebhookEndpointOut:
    ep = await db.get(WebhookEndpoint, endpoint_id)
    if ep is None:
        raise NotFound("Endpoint not found")
    ep.url, ep.events, ep.active = str(body.url), body.events, body.active
    await db.commit()
    return WebhookEndpointOut.model_validate(ep)


@router.delete("/webhook-endpoints/{endpoint_id}", response_model=Ok)
async def delete_endpoint(endpoint_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> Ok:
    ep = await db.get(WebhookEndpoint, endpoint_id)
    if ep is None:
        raise NotFound("Endpoint not found")
    await db.delete(ep)
    await db.commit()
    return Ok()


@router.post("/webhook-endpoints/{endpoint_id}/test", response_model=Ok)
async def test_endpoint(endpoint_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> Ok:
    ep = await db.get(WebhookEndpoint, endpoint_id)
    if ep is None:
        raise NotFound("Endpoint not found")
    emit(
        db,
        principal.org_id,
        "webhook.deliver",
        {"endpoint_id": ep.id, "event": "ping", "event_id": str(uuid.uuid4()), "data": {"hello": "anvita"}},
    )
    await db.commit()
    return Ok()


# ---- Billing ----------------------------------------------------------------------------------------


def _billing_provider_configured() -> bool:
    return any(s.credentials() for s in providers.specs("billing"))


@router.get("/billing", response_model=BillingOut)
async def billing_overview(principal: CurrentPrincipal, db: DB) -> BillingOut:
    invoices = await db.scalars(
        select(BillingInvoice)
        .where(BillingInvoice.org_id == principal.org_id)
        .order_by(BillingInvoice.issued_at.desc())
        .limit(24)
    )
    return BillingOut(
        plan=principal.org.plan,
        plans=[
            PlanOut(id=p.id, name=p.name, price_minor=p.price_minor, who=p.who, features=list(p.features))
            for p in billing.PLANS
            if p.id != "trial"
        ],
        meters=[MeterOut.model_validate(m) for m in await billing.meters(db, principal.org_id, principal.org.plan)],
        credit_packs=[CreditPackOut(id=k, label=v[0], price_minor=v[3]) for k, v in billing.CREDIT_PACKS.items()],
        invoices=[InvoiceOut.model_validate(i) for i in invoices],
        checkout_provider_configured=_billing_provider_configured(),
    )


async def _checkout_or_apply(db: DB, principal: Any, what: str, amount_minor: int, apply) -> CheckoutOut:  # noqa: ANN001
    """With a billing provider, return its checkout URL (the webhook applies the change after payment).
    Without one, owners can apply changes directly outside production (free plans / manual invoicing)."""
    if principal.role != "owner":
        raise Forbidden("Only the workspace owner can change billing")
    if _billing_provider_configured():
        adapter = providers.resolve("billing")
        url = await adapter.checkout(
            org_id=str(principal.org_id), plan=what, success_url=f"{get_settings().app_base_url}/settings?tab=billing"
        )
        return CheckoutOut(applied=False, checkout_url=url)
    if get_settings().is_production:
        raise providers.ProviderNotConfigured(
            "Billing provider is not configured", details={"kind": "billing", "env": ["ANVITA_STRIPE_SECRET_KEY"]}
        )
    await apply()
    count = (
        await db.scalar(
            select(func.count()).select_from(BillingInvoice).where(BillingInvoice.org_id == principal.org_id)
        )
        or 0
    )
    db.add(
        BillingInvoice(
            org_id=principal.org_id,
            number=f"AN-{utcnow():%Y}-{count + 1:04d}",
            amount_minor=round(amount_minor * 1.18),
            items=f"{what} + 18% GST",
            status="paid",
        )
    )
    await db.commit()
    return CheckoutOut(applied=True)


@router.post("/billing/plan", response_model=CheckoutOut)
async def change_plan(body: ChangePlanIn, principal: CurrentPrincipal, db: DB) -> CheckoutOut:
    plan = billing.PLANS_BY_ID.get(body.plan)
    if plan is None or plan.id == "trial":
        raise NotFound("Unknown plan")

    async def apply() -> None:
        principal.org.plan = plan.id
        audit(db, principal.org_id, "billing.plan_changed", actor_id=principal.user_id, data={"plan": plan.id})

    return await _checkout_or_apply(db, principal, f"{plan.name} plan", plan.price_minor, apply)


@router.post("/billing/credits", response_model=CheckoutOut)
async def buy_credits(body: BuyCreditsIn, principal: CurrentPrincipal, db: DB) -> CheckoutOut:
    pack = billing.CREDIT_PACKS.get(body.pack)
    if pack is None:
        raise NotFound("Unknown credit pack")
    label, meter, amount, price = pack

    async def apply() -> None:
        db.add(CreditLedger(org_id=principal.org_id, meter=meter, delta=amount, reason=f"Purchased {label}"))

    return await _checkout_or_apply(db, principal, label, price, apply)


# ---- Compliance (DPDP / GDPR) -----------------------------------------------------------------------


@router.get("/data-requests", response_model=list[DataRequestOut])
async def list_requests(principal: AdminPrincipal, db: DB) -> list[DataRequestOut]:
    rows = await db.scalars(
        select(DataRequest).where(DataRequest.org_id == principal.org_id).order_by(DataRequest.created_at.desc())
    )
    return [DataRequestOut.model_validate(r) for r in rows]


@router.post("/data-requests", response_model=DataRequestOut, status_code=201)
async def create_request(body: DataRequestIn, principal: AdminPrincipal, db: DB) -> DataRequestOut:
    count = (
        await db.scalar(select(func.count()).select_from(DataRequest).where(DataRequest.org_id == principal.org_id))
        or 0
    )
    contact = await compliance.find_subject(db, principal.org_id, body.subject, principal.org.country)
    req = DataRequest(
        org_id=principal.org_id,
        number=f"DSR-{count + 1:04d}",
        type=body.type,
        subject=body.subject,
        contact_id=contact.id if contact else None,
        note=body.note,
        due_at=utcnow() + timedelta(days=30),
    )  # DPDP/GDPR: respond within a month
    db.add(req)
    audit(
        db,
        principal.org_id,
        "data_request.created",
        actor_id=principal.user_id,
        data={"type": body.type, "subject": body.subject},
    )
    await db.commit()
    return DataRequestOut.model_validate(req)


@router.post("/data-requests/{request_id}/process", response_model=DataRequestOut)
async def process_request(request_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> DataRequestOut:
    req = await db.get(DataRequest, request_id, with_for_update=True)
    if req is None:
        raise NotFound("Request not found")
    if req.status == "completed":
        raise AppError("Request already completed", code="already_completed")
    await compliance.process(db, req, principal.user_id)
    await db.commit()
    return DataRequestOut.model_validate(req)


@router.get("/data-requests/{request_id}/export", response_model=dict)
async def download_export(request_id: uuid.UUID, principal: AdminPrincipal, db: DB) -> dict:
    req = await db.get(DataRequest, request_id)
    if req is None or req.type != "export" or req.result is None:
        raise NotFound("No export available")
    return req.result


# ---- Notification preferences -----------------------------------------------------------------------


@router.get("/me/notification-preferences", response_model=NotificationPrefs)
async def get_prefs(principal: CurrentPrincipal) -> NotificationPrefs:
    return NotificationPrefs(preferences=principal.membership.preferences or {})


@router.put("/me/notification-preferences", response_model=NotificationPrefs)
async def put_prefs(body: NotificationPrefs, principal: CurrentPrincipal, db: DB) -> NotificationPrefs:
    principal.membership.preferences = body.preferences
    await db.commit()
    return NotificationPrefs(preferences=body.preferences)

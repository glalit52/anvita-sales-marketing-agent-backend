import hashlib
import hmac
import json

import httpx

from app.core.config import get_settings
from app.db.session import tenant_session
from app.models import WebhookEndpoint
from app.services import webhooks_out
from app.worker import drain


async def test_integrations_catalog_and_connect(ws):
    items = {i["id"]: i for i in (await ws.get("/integrations")).json()}
    assert items["apollo"]["auth"] == "api_key" and items["apollo"]["fields"] == ["ANVITA_APOLLO_API_KEY"]
    assert items["webhooks"]["connected"] is False

    missing = await ws.post("/integrations/apollo/connect", {"credentials": {}})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "missing_credentials"
    ok = await ws.post(
        "/integrations/apollo/connect", {"account": "Team plan", "credentials": {"ANVITA_APOLLO_API_KEY": "k-123"}}
    )
    assert ok.status_code == 200 and ok.json()["connected"] is True
    # Credentials are never echoed back.
    assert "k-123" not in ok.text

    oauth = await ws.post("/integrations/salesforce/connect", {})
    assert oauth.status_code == 501 and oauth.json()["error"]["code"] == "provider_not_configured"
    assert (await ws.post("/integrations/apollo/disconnect")).status_code == 200


async def test_api_key_auth_and_scopes(client, ws):
    r = await ws.post("/api-keys", {"name": "Booking form", "scopes": ["contacts:read", "contacts:write"]})
    assert r.status_code == 201
    raw = r.json()["key"]
    assert raw.startswith("anv_live_")
    h = {"X-Api-Key": raw}
    created = await client.post("/contacts", json={"name": "From website", "phone": "+919800000001"}, headers=h)
    assert created.status_code == 201
    assert (await client.get("/contacts", headers=h)).json()["total"] == 1
    denied = await client.get("/payments", headers=h)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "insufficient_scope"
    # Keys never get admin powers even when created by the owner.
    assert (await client.get("/api-keys", headers={"Authorization": f"Bearer {raw}"})).status_code == 403

    await ws.delete(f"/api-keys/{r.json()['id']}")
    assert (await client.get("/contacts", headers=h)).status_code == 401


async def test_outgoing_webhooks_are_signed_and_delivered(client, ws, monkeypatch):
    ep = await ws.post("/webhook-endpoints", {"url": "https://hooks.example.com/anvita", "events": ["contact.created"]})
    assert ep.status_code == 201
    secret = ep.json()["secret"]
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    real_deliver = webhooks_out.deliver

    async def fake_deliver(db, endpoint, event, event_id, data, client=None):  # noqa: ANN001, ANN202
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await real_deliver(db, endpoint, event, event_id, data, client=c)

    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(webhooks_out, "assert_public_url", public)
    monkeypatch.setattr("app.worker.deliver", fake_deliver)

    await ws.post("/contacts", {"name": "Hook Test", "phone": "+919800000002"})
    await drain()
    assert len(received) == 1
    req = received[0]
    body = req.content
    assert json.loads(body)["type"] == "contact.created" and json.loads(body)["data"]["name"] == "Hook Test"
    ts = req.headers["x-anvita-timestamp"]
    expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert req.headers["x-anvita-signature"] == expected
    async with tenant_session(ws.org_uuid) as db:
        stored = await db.get(WebhookEndpoint, __import__("uuid").UUID(ep.json()["id"]))
        assert stored.success_count == 1


async def test_billing_plan_change_and_credits(ws):
    b = (await ws.get("/billing")).json()
    assert b["plan"] == "trial" and {m["id"] for m in b["meters"]} == {"ai", "prospect", "voice", "wa"}
    r = await ws.post("/billing/plan", {"plan": "growth"})
    assert r.json() == {"applied": True, "checkoutUrl": None}
    await ws.post("/billing/credits", {"pack": "prospect1000"})
    b = (await ws.get("/billing")).json()
    assert b["plan"] == "growth"
    prospect = next(m for m in b["meters"] if m["id"] == "prospect")
    assert prospect["limit"] == 2000  # 1000 included + 1000 purchased
    assert len(b["invoices"]) == 2


async def test_data_request_export_and_erase(ws):
    c = (
        await ws.post("/contacts", {"name": "Meera Nair", "phone": "+919730055667", "email": "meera@example.com"})
    ).json()
    await ws.post(f"/contacts/{c['id']}/notes", {"body": "VIP"})
    exp = (await ws.post("/data-requests", {"type": "export", "subject": "meera@example.com"})).json()
    assert exp["contactId"] == c["id"]
    done = (await ws.post(f"/data-requests/{exp['id']}/process")).json()
    assert done["status"] == "completed"
    data = (await ws.get(f"/data-requests/{exp['id']}/export")).json()
    assert data["contact"]["name"] == "Meera Nair" and data["notes"][0]["body"] == "VIP"

    dele = (await ws.post("/data-requests", {"type": "delete", "subject": "+91 97300 55667"})).json()
    await ws.post(f"/data-requests/{dele['id']}/process")
    assert (await ws.get(f"/contacts/{c['id']}")).status_code == 404


async def test_provider_slot_reports_missing_config(ws, monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_sample_providers", False)
    from app.services import providers

    try:
        providers.resolve("people_search")
    except providers.ProviderNotConfigured as e:
        assert e.details["options"][0]["env"] == ["ANVITA_APOLLO_API_KEY"]
    else:
        raise AssertionError("expected ProviderNotConfigured")
    status = {p["id"]: p for p in (await ws.get("/platform/providers")).json()}
    assert status["apollo"]["configured"] is False and status["smtp"]["paid"] is False

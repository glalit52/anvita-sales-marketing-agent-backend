import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta

from app.core.config import get_settings
from app.worker import drain


async def _first_slot(ws, clinic):
    start = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    slots = (
        await ws.get("/availability", params={"serviceId": clinic["service"]["id"], "start": start, "days": 2})
    ).json()
    assert slots, "expected free slots"
    return slots


async def test_availability_and_double_booking_guard(ws, clinic):
    contact = (await ws.post("/contacts", {"name": "Sneha Iyer"})).json()
    slots = await _first_slot(ws, clinic)
    s0 = slots[0]
    body = {
        "contactId": contact["id"],
        "serviceId": clinic["service"]["id"],
        "locationId": clinic["location"]["id"],
        "providerId": clinic["provider"]["id"],
        "startsAt": s0["startsAt"],
    }
    first = await ws.post("/appointments", body)
    assert first.status_code == 201, first.text
    assert (await ws.get(f"/contacts/{contact['id']}")).json()["stage"] == "booked"

    # The database exclusion constraint rejects an overlapping booking for the same provider.
    clash = await ws.post("/appointments", body)
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "slot_taken"

    after = (
        await ws.get(
            "/availability", params={"serviceId": clinic["service"]["id"], "start": s0["startsAt"][:10], "days": 1}
        )
    ).json()
    assert s0["startsAt"] not in [s["startsAt"] for s in after]

    # Rescheduling frees the old slot.
    moved = await ws.patch(f"/appointments/{first.json()['id']}", {"startsAt": slots[3]["startsAt"]})
    assert moved.status_code == 200, moved.text
    rng = {"start": (datetime.now(UTC)).isoformat(), "end": (datetime.now(UTC) + timedelta(days=4)).isoformat()}
    active = [a for a in (await ws.get("/appointments", params=rng)).json() if a["status"] != "cancelled"]
    assert [a["startsAt"] for a in active] == [moved.json()["startsAt"]]


async def test_public_booking_page_creates_contact_and_deposit(client, ws, clinic):
    slug = ws.org["slug"]
    biz = (await client.get(f"/public/orgs/{slug}")).json()
    assert biz["services"][0]["name"] == "Whitening consultation"
    start = (date.today() + timedelta(days=2)).isoformat()
    slots = (
        await client.get(
            f"/public/orgs/{slug}/availability", params={"serviceId": clinic["service"]["id"], "start": start}
        )
    ).json()
    r = await client.post(
        f"/public/orgs/{slug}/bookings",
        json={
            "name": "Tanvi Gokhale",
            "phone": "98901 45612",
            "serviceId": clinic["service"]["id"],
            "locationId": clinic["location"]["id"],
            "startsAt": slots[0]["startsAt"],
            "whatsappOptIn": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["payUrl"]
    contacts = (await ws.get("/contacts", params={"q": "Tanvi"})).json()["items"]
    assert contacts[0]["phone"] == "+919890145612" and contacts[0]["stage"] == "booked"

    # A time outside working hours is refused for online bookings.
    bad = await client.post(
        f"/public/orgs/{slug}/bookings",
        json={
            "name": "Night Owl",
            "phone": "9000000000",
            "serviceId": clinic["service"]["id"],
            "locationId": clinic["location"]["id"],
            "startsAt": f"{start}T23:30:00+05:30",
        },
    )
    assert bad.status_code == 409


async def test_payment_link_lifecycle(client, ws):
    contact = (await ws.post("/contacts", {"name": "Rohit Mehta"})).json()
    p = await ws.post(
        "/payments",
        {
            "contactId": contact["id"],
            "purpose": "Root canal",
            "lines": [{"description": "Root canal", "quantity": 1, "unitMinor": 1500000, "taxPct": 18}],
        },
    )
    assert p.status_code == 201, p.text
    pay = p.json()
    assert pay["amountMinor"] == 1770000 and pay["status"] == "pending"
    token = pay["payUrl"].rsplit("/", 1)[1]

    public = (await client.get(f"/public/pay/{token}")).json()
    assert public["businessName"] == "Smile Dental" and public["customerName"] == "Rohit Mehta"
    assert public["provider"] == "test" and public["checkoutUrl"] is None
    paid = await client.post(f"/public/pay/{token}/test-confirm", json={"method": "upi"})
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    # Confirming twice is harmless.
    assert (await client.post(f"/public/pay/{token}/test-confirm", json={"method": "upi"})).status_code == 200

    summary = (await ws.get("/payments/summary")).json()
    assert summary["collectedMinor"] == 1770000 and summary["paidCount"] == 1
    notes = (await ws.get("/notifications")).json()
    assert notes[0]["icon"] == "payment"
    assert (await ws.post(f"/payments/{pay['id']}/refund")).json()["status"] == "refunded"
    dash = (await ws.get("/dashboard", params={"days": 7})).json()
    assert len(dash["trend"]) == 7 and dash["leads"] == 1
    await drain()


async def test_razorpay_webhook_requires_signature(client, ws, monkeypatch):
    contact = (await ws.post("/contacts", {"name": "Harish"})).json()
    pay = (
        await ws.post("/payments", {"contactId": contact["id"], "purpose": "Annual plan", "amountMinor": 41000000})
    ).json()
    from app.db.session import tenant_session
    from app.models import Payment

    async with tenant_session(ws.org_uuid) as db:
        p = await db.get(Payment, __import__("uuid").UUID(pay["id"]))
        p.provider, p.provider_order_id = "razorpay", "plink_123"
        await db.commit()

    secret = "whsec_test"
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", __import__("pydantic").SecretStr(secret))
    event = json.dumps(
        {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": "plink_123"}},
                "payment": {"entity": {"id": "pay_9", "method": "upi"}},
            },
        }
    )
    bad = await client.post("/webhooks/razorpay", content=event, headers={"x-razorpay-signature": "nope"})
    assert bad.status_code == 401
    sig = hmac.new(secret.encode(), event.encode(), hashlib.sha256).hexdigest()
    ok = await client.post("/webhooks/razorpay", content=event, headers={"x-razorpay-signature": sig})
    assert ok.status_code == 200
    assert (await ws.get(f"/payments/{pay['id']}")).json()["status"] == "paid"

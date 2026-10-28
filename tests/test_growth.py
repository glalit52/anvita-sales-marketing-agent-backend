import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.session import tenant_session
from app.models import Contact, Enrollment, Message, ReviewRequest, Task
from app.services import sequences
from app.services.inbox import record_inbound
from app.worker import drain


async def _contact(ws, name: str, phone: str, *, email: str | None = None, consent: list[str] = (), **extra) -> str:
    r = await ws.post("/contacts", {"name": name, "phone": phone, "email": email, **extra})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    for ch in consent:
        ok = await ws.put(f"/contacts/{cid}/consents", {"channel": ch, "status": "opted_in", "source": "Signup form"})
        assert ok.status_code == 200, ok.text
    return cid


async def test_reviews_triage_reply_and_stats(ws):
    good = await ws.post("/reviews", {"platform": "google", "author": "Ananya Rao", "rating": 5, "text": "Lovely"})
    assert good.status_code == 201, good.text
    assert good.json()["sentiment"] == "positive" and "Ananya" in good.json()["aiDraft"]
    bad = (
        await ws.post("/reviews", {"platform": "practo", "author": "Vikram", "rating": 1, "text": "Waited 1h"})
    ).json()
    assert bad["flagged"]

    flagged = (await ws.get("/reviews", params={"status": "flagged"})).json()
    assert [r["id"] for r in flagged] == [bad["id"]]
    replied = await ws.post(f"/reviews/{bad['id']}/reply", {"text": "Sorry Vikram, please call us."})
    assert replied.json()["reply"] and replied.json()["flagged"] is None

    stats = (await ws.get("/reviews/stats")).json()
    assert stats["total"] == 2 and stats["average"] == 3.0 and stats["needsReply"] == 1
    notes = (await ws.get("/notifications")).json()
    assert any("1★" in n["title"] for n in (notes["items"] if isinstance(notes, dict) else notes))


async def test_review_request_feedback_funnel(client, ws):
    await ws.put(
        "/reviews/settings",
        {"autoRequest": True, "delayHours": 0, "channel": "sms", "reviewLinks": {"google": "https://g.page/r/x"}},
    )
    happy = await _contact(ws, "Meera Iyer", "+919811100001")
    unhappy = await _contact(ws, "Rohit Jain", "+919811100002")
    sent = await ws.post("/review-requests", {"contactIds": [happy, unhappy], "channel": "sms"})
    assert sent.json() == {"sent": 2, "skipped": []}

    async with tenant_session(ws.org_uuid) as db:
        tokens = {str(r.contact_id): r.token for r in (await db.scalars(select(ReviewRequest))).all()}
        bodies = (await db.scalars(select(Message.body))).all()
    assert any(f"/feedback/{tokens[happy]}" in b for b in bodies)

    page = await client.get(f"/public/feedback/{tokens[happy]}")
    assert page.status_code == 200 and page.json()["firstName"] == "Meera"
    rated = (await client.post(f"/public/feedback/{tokens[happy]}/rate", json={"rating": 5})).json()
    assert rated["publicLink"] == "https://g.page/r/x" and set(rated["suggestions"]) == {
        "short",
        "detailed",
        "hinglish",
    }
    assert (await client.post(f"/public/feedback/{tokens[happy]}/posted")).json()["status"] == "reviewed"

    low = (await client.post(f"/public/feedback/{tokens[unhappy]}/rate", json={"rating": 2})).json()
    assert low["publicLink"] is None
    priv = await client.post(
        f"/public/feedback/{tokens[unhappy]}/private", json={"issues": ["Wait time", "bogus"], "text": "Too slow"}
    )
    assert priv.status_code == 200
    again = await client.post(f"/public/feedback/{tokens[unhappy]}/private", json={"issues": []})
    assert again.status_code == 409
    assert (await client.get("/public/feedback/nope")).status_code == 404

    funnel = (await ws.get("/review-requests/funnel")).json()
    assert funnel == {"sent": 2, "clicked": 1, "ratedHigh": 1, "reviewed": 1, "privateFeedback": 1}
    async with tenant_session(ws.org_uuid) as db:
        task = await db.scalar(select(Task).where(Task.contact_id == uuid.UUID(unhappy)))
        req = await db.scalar(select(ReviewRequest).where(ReviewRequest.contact_id == uuid.UUID(unhappy)))
    assert task and task.priority == "high" and req.issues == ["Wait time"]


async def test_completed_appointment_triggers_review_request(ws, clinic):
    await ws.put("/reviews/settings", {"autoRequest": True, "delayHours": 0, "channel": "sms", "reviewLinks": {}})
    cid = await _contact(ws, "Kabir Shah", "+919811100003")
    start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
    appt = await ws.post(
        "/appointments",
        {
            "contactId": cid,
            "serviceId": clinic["service"]["id"],
            "locationId": clinic["location"]["id"],
            "startsAt": start.isoformat(),
        },
    )
    assert appt.status_code == 201, appt.text
    await ws.patch(f"/appointments/{appt.json()['id']}", {"status": "completed"})
    await drain()
    reqs = (await ws.get("/review-requests")).json()
    assert len(reqs) == 1 and reqs[0]["contactName"] == "Kabir Shah"
    await drain()  # idempotent: no second request for the same visit
    assert len((await ws.get("/review-requests")).json()) == 1


async def test_segments_and_campaign_respects_consent(ws):
    a = await _contact(ws, "Asha", "+919811100010", consent=["sms"], city="Pune")
    await _contact(ws, "Bina", "+919811100011", city="Pune")  # no consent → skipped
    await _contact(ws, "Chetan", "+919811100012", consent=["sms"], city="Mumbai")
    rules = {"match": "all", "conditions": [{"field": "city", "op": "eq", "value": "Pune"}]}
    preview = (await ws.post("/segments/preview", {"name": "Pune", "rules": rules})).json()
    assert preview["count"] == 2
    bad = await ws.post("/segments/preview", {"name": "x", "rules": {"conditions": [{"field": "nope"}]}})
    assert bad.status_code == 400
    seg = (await ws.post("/segments", {"name": "Pune", "rules": rules})).json()
    assert (await ws.get("/segments")).json()[0]["count"] == 2

    camp = await ws.post(
        "/campaigns",
        {"name": "Diwali offer", "channel": "sms", "segmentId": seg["id"], "body": "Hi {{first_name}}, 20% off!"},
    )
    assert camp.status_code == 201, camp.text
    cid = camp.json()["id"]
    assert (await ws.get(f"/campaigns/{cid}/audience")).json() == {"eligible": 1, "total": 2}
    launched = (await ws.post(f"/campaigns/{cid}/launch")).json()
    assert launched["skippedNoConsent"] == 1
    await drain()
    done = (await ws.get(f"/campaigns/{cid}")).json()
    assert done["status"] == "sent" and done["stats"]["sent"] >= 1
    async with tenant_session(ws.org_uuid) as db:
        msgs = (await db.scalars(select(Message).where(Message.direction == "outbound"))).all()
    assert [m.body for m in msgs] == ["Hi Asha, 20% off!"]
    assert str(msgs[0].org_id) == ws.org["id"] and a

    wa = await ws.post("/campaigns", {"name": "WA", "channel": "whatsapp", "segmentId": seg["id"]})
    blocked = await ws.post(f"/campaigns/{wa.json()['id']}/launch")
    assert blocked.status_code == 400 and blocked.json()["error"]["code"] == "template_not_approved"


async def test_templates_lifecycle(ws):
    t = await ws.post(
        "/templates", {"name": "winback_1", "channel": "whatsapp", "body": "Hi {{1}}, we miss you at {{2}}"}
    )
    assert t.status_code == 201 and t.json()["status"] == "draft"
    tid = t.json()["id"]
    sub = (await ws.post(f"/templates/{tid}/submit")).json()
    assert sub["status"] in ("pending", "approved")
    ok = (await ws.put(f"/templates/{tid}/status", {"status": "approved"})).json()
    assert ok["status"] == "approved"


async def test_sequence_enroll_run_and_stop_on_reply(ws):
    cid = await _contact(ws, "Nikhil Rao", "+919811100020", email="nikhil@acme.in", company="Acme")
    seq = await ws.post(
        "/sequences",
        {
            "name": "Clinic owners",
            "settings": {"days": [0, 1, 2, 3, 4, 5, 6], "sendFrom": "00:00", "sendTo": "23:59"},
            "steps": [
                {
                    "type": "email",
                    "day": 0,
                    "title": "Intro",
                    "subject": "Quick idea",
                    "body": "Hi {{first_name}}. {{ai_first_line}}",
                },
                {"type": "call", "day": 2, "title": "Call {{first_name}}", "body": "Follow up"},
            ],
        },
    )
    assert seq.status_code == 201, seq.text
    sid = seq.json()["id"]
    assert (await ws.post(f"/sequences/{sid}/enroll", {"contactIds": [cid]})).json() == {"enrolled": 1, "skipped": 0}
    assert (await ws.post(f"/sequences/{sid}/enroll", {"contactIds": [cid]})).json()["skipped"] == 1
    active = (await ws.post(f"/sequences/{sid}/status", params={"status": "active"})).json()
    assert active["status"] == "active"

    async with tenant_session(ws.org_uuid) as db:
        e = await db.scalar(select(Enrollment))
        assert e.next_run_at is not None
        assert await sequences.run_step(db, e.id, 0) == "sent"
        assert await sequences.run_step(db, e.id, 0) == "stale"  # duplicate delivery
        await db.commit()
        msg = await db.scalar(select(Message).where(Message.direction == "outbound"))
        assert msg.body.startswith("Hi Nikhil.") and "Acme" in msg.body

        contact = await db.get(Contact, uuid.UUID(cid))
        await record_inbound(
            db, ws.org_uuid, contact, channel_type="email", channel_id=None, body="Please unsubscribe me"
        )
        await db.commit()
    await drain()
    rows = (await ws.get(f"/sequences/{sid}/enrollments")).json()
    assert rows[0]["status"] == "unsubscribed" and rows[0]["replyClass"] == "Unsubscribe"
    consents = (await ws.get(f"/contacts/{cid}/consents")).json()
    assert {c["channel"]: c["status"] for c in consents}["email"] == "opted_out"
    detail = (await ws.get(f"/sequences/{sid}")).json()
    assert detail["steps"][0]["stats"].get("sent") == 1


def test_next_send_time_respects_window():
    cfg = {"send_from": "09:00", "send_to": "18:00", "days": [0, 1, 2, 3, 4], "daily_cap": 60, "stop_on_reply": True}
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    sat_night = datetime(2026, 10, 31, 22, 0, tzinfo=tz)  # Saturday
    t = sequences.next_send_time(sat_night, cfg, tz).astimezone(tz)
    assert t.weekday() == 0 and 9 <= t.hour < 18
